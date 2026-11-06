#!/usr/bin/env bash
# Deploys the recorder to a small always-on AWS instance, with a daily sync to a private S3 bucket
# and a phone alert through ntfy when no data is written for 10 minutes. Safe to run again: it
# creates what is missing and updates the rest.
#
# Requires Docker and the AWS CLI with working credentials. Settings can be set in the environment
# or in deploy.env, which is not committed: AWS_REGION, INSTANCE_TYPE, DISK_GB, PRODUCTS, NTFY_TOPIC,
# and BUDGET_EMAIL, the address for budget alerts, which is required.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

ensure_topic() {
    # ntfy topics have no password: the name is the secret, so it is long and random.
    if [ -z "${NTFY_TOPIC:-}" ]; then
        NTFY_TOPIC=touchstone-$(openssl rand -hex 16)
        (umask 077 && echo "NTFY_TOPIC=${NTFY_TOPIC}" >>"$DEPLOY_ENV")
        log "created a private alert topic and saved it to ${DEPLOY_ENV}"
    fi
}

ensure_bucket() {
    log "bucket ${BUCKET}"
    if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
        if [ "$AWS_REGION" = us-east-1 ]; then
            aws s3api create-bucket --bucket "$BUCKET" >/dev/null
        else
            aws s3api create-bucket --bucket "$BUCKET" \
                --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
        fi
    fi
    aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
    aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration '{
        "Rules": [
            {"ID": "ops", "Status": "Enabled", "Filter": {"Prefix": "ops/"},
             "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
            {"ID": "deploy", "Status": "Enabled", "Filter": {"Prefix": "deploy/"},
             "NoncurrentVersionExpiration": {"NoncurrentDays": 7}}
        ]}'
}

ensure_role() {
    local name=$1 service=$2
    aws iam get-role --role-name "$name" >/dev/null 2>&1 ||
        aws iam create-role --role-name "$name" --assume-role-policy-document \
            "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"${service}\"},\"Action\":\"sts:AssumeRole\"}]}" >/dev/null
}

ensure_instance_role() {
    log "instance role"
    ensure_role "$NAME" ec2.amazonaws.com
    aws iam attach-role-policy --role-name "$NAME" \
        --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    # The server can add to the bucket but has no permission to delete from it.
    aws iam put-role-policy --role-name "$NAME" --policy-name s3 --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
            {\"Effect\": \"Allow\", \"Action\": [\"s3:GetObject\", \"s3:PutObject\"], \"Resource\": \"arn:aws:s3:::${BUCKET}/*\"},
            {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::${BUCKET}\"}
        ]}"
    if ! aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
        aws iam create-instance-profile --instance-profile-name "$NAME" >/dev/null
        aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
    fi
}

ensure_security_group() {
    local vpc
    vpc=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
    [ "$vpc" != None ] || die "no default VPC in ${AWS_REGION}"
    SECURITY_GROUP=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=${NAME}" "Name=vpc-id,Values=${vpc}" \
        --query 'SecurityGroups[0].GroupId' --output text)
    if [ "$SECURITY_GROUP" = None ]; then
        # A new group allows nothing inbound and everything outbound, which is all that is needed.
        SECURITY_GROUP=$(aws ec2 create-security-group --group-name "$NAME" --vpc-id "$vpc" \
            --description "Touchstone recorder: no inbound access" --query GroupId --output text)
    fi
}

launch_instance() {
    local attempt
    # A new instance profile takes a few seconds to become usable.
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if INSTANCE_ID=$(aws ec2 run-instances \
            --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
            --instance-type "$INSTANCE_TYPE" \
            --iam-instance-profile "Name=${NAME}" \
            --security-group-ids "$SECURITY_GROUP" \
            --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":${DISK_GB},\"VolumeType\":\"gp3\",\"Encrypted\":true,\"DeleteOnTermination\":false}}]" \
            --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1 \
            --disable-api-termination \
            --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}}]" \
            "ResourceType=volume,Tags=[{Key=Name,Value=${NAME}}]" \
            --user-data file://scripts/server/bootstrap.sh \
            --query 'Instances[0].InstanceId' --output text); then
            return
        fi
        log "waiting for the instance profile (attempt ${attempt})"
        sleep "${RETRY_DELAY_S:-10}"
    done
    die "could not launch the instance"
}

ensure_instance() {
    INSTANCE_ID=$(find_instance)
    if [ "$INSTANCE_ID" = None ]; then
        log "launching ${INSTANCE_TYPE}"
        launch_instance
    elif [ "$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)" = stopped ]; then
        aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
    fi
    log "instance ${INSTANCE_ID}: waiting for it to come online"
    local status attempt
    for attempt in $(seq 1 60); do
        status=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
            --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo None)
        [ "$status" = Online ] && break
        [ "$attempt" = 60 ] && die "instance did not come online"
        sleep "${RETRY_DELAY_S:-10}"
    done
    run_remote 'cloud-init status --wait >/dev/null; cloud-init status' >/dev/null
}

ship_release() {
    local tag dirty=""
    git diff --quiet HEAD -- . || dirty=-dirty
    tag=$(git rev-parse --short HEAD)${dirty}-$(date -u +%Y%m%d%H%M%S)
    log "building and uploading image ${tag}"
    docker build --quiet --platform linux/arm64 -t "touchstone-recorder:${tag}" . >/dev/null
    docker save "touchstone-recorder:${tag}" | gzip | aws s3 cp - "s3://${BUCKET}/deploy/recorder-${tag}.tar.gz" --only-show-errors
    aws s3 sync scripts/server "s3://${BUCKET}/deploy/server/" --delete --only-show-errors

    log "installing on the server"
    run_remote "umask 077; mkdir -p /etc/touchstone; cat >/etc/touchstone/env <<SETTINGS
TOUCHSTONE_BUCKET=${BUCKET}
TOUCHSTONE_PRODUCTS=${PRODUCTS}
AWS_REGION=${AWS_REGION}
NTFY_TOPIC=${NTFY_TOPIC}
DATA_DIR=/var/lib/touchstone/data
SETTINGS
AWS_REGION=${AWS_REGION} aws s3 cp s3://${BUCKET}/deploy/server/install.sh /tmp/touchstone-install.sh --only-show-errors && bash /tmp/touchstone-install.sh ${tag}"
}

# The alert function treats a product with no writes as stopped, so it must not start until the
# first data for every product is on disk and reported.
wait_for_first_data() {
    log "waiting for the first data from every product"
    local attempt
    for attempt in $(seq 1 60); do
        if aws s3 cp "s3://${BUCKET}/ops/heartbeat.json" - 2>/dev/null |
            PRODUCTS=$PRODUCTS python3 -c 'import json, os, sys; written = json.load(sys.stdin)["products"]; sys.exit(not all(written.get(p) for p in os.environ["PRODUCTS"].split(",")))' 2>/dev/null; then
            return
        fi
        [ "$attempt" = 60 ] && die "no data reported within the time allowed"
        sleep "${RETRY_DELAY_S:-10}"
    done
}

ensure_alert() {
    log "alert function"
    local function_name=touchstone-alert role_arn package attempt rule_arn function_arn settings
    ensure_role "$function_name" lambda.amazonaws.com
    aws iam attach-role-policy --role-name "$function_name" \
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    aws iam put-role-policy --role-name "$function_name" --policy-name s3 --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
            {\"Effect\": \"Allow\", \"Action\": [\"s3:GetObject\", \"s3:PutObject\"], \"Resource\": \"arn:aws:s3:::${BUCKET}/ops/*\"},
            {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::${BUCKET}\"},
            {\"Effect\": \"Allow\", \"Action\": \"budgets:ViewBudget\", \"Resource\": \"arn:aws:budgets::${ACCOUNT}:budget/${BUDGET_NAME}\"}
        ]}"
    role_arn=$(aws iam get-role --role-name "$function_name" --query Role.Arn --output text)

    package=$(mktemp -d)/alert.zip
    python3 -m zipfile -c "$package" src/touchstone/recorder/alert.py
    settings="{\"Variables\":{\"BUCKET\":\"${BUCKET}\",\"NTFY_TOPIC\":\"${NTFY_TOPIC}\",\"PRODUCTS\":\"${PRODUCTS}\",\"ACCOUNT_ID\":\"${ACCOUNT}\",\"BUDGET_NAME\":\"${BUDGET_NAME}\"}}"
    if aws lambda get-function --function-name "$function_name" >/dev/null 2>&1; then
        aws lambda update-function-code --function-name "$function_name" --zip-file "fileb://${package}" >/dev/null
        aws lambda wait function-updated --function-name "$function_name"
        aws lambda update-function-configuration --function-name "$function_name" --environment "$settings" >/dev/null
    else
        # A new role takes a few seconds before Lambda can use it.
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            aws lambda create-function --function-name "$function_name" --runtime python3.12 \
                --architectures arm64 --handler alert.handler --role "$role_arn" --timeout 30 \
                --zip-file "fileb://${package}" --environment "$settings" >/dev/null && break
            [ "$attempt" = 10 ] && die "could not create the alert function"
            sleep "${RETRY_DELAY_S:-10}"
        done
    fi

    rule_arn=$(aws events put-rule --name "$function_name" --schedule-expression 'rate(1 minute)' \
        --query RuleArn --output text)
    function_arn=$(aws lambda get-function --function-name "$function_name" --query Configuration.FunctionArn --output text)
    aws lambda add-permission --function-name "$function_name" --statement-id events \
        --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn "$rule_arn" >/dev/null 2>&1 || true
    aws events put-targets --rule "$function_name" --targets "Id=1,Arn=${function_arn}" >/dev/null
}

# Spend is counted before credits, because credits are what it uses up. The budget is yearly
# rather than monthly, because the credits are a single amount, not a monthly allowance.
ensure_budget() {
    log "budget ${BUDGET_NAME}: alerts at \$25, \$50 and \$80, and a stop at \$90"
    if ! aws budgets describe-budget --account-id "$ACCOUNT" --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
        local alerts="" kind amount
        for alert in ACTUAL:25 ACTUAL:50 ACTUAL:80 FORECASTED:80; do
            kind=${alert%%:*} amount=${alert##*:}
            alerts+="${alerts:+,}{\"Notification\":{\"NotificationType\":\"${kind}\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":${amount},\"ThresholdType\":\"ABSOLUTE_VALUE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"${BUDGET_EMAIL}\"}]}"
        done
        aws budgets create-budget --account-id "$ACCOUNT" --budget "{
            \"BudgetName\": \"${BUDGET_NAME}\", \"BudgetType\": \"COST\", \"TimeUnit\": \"ANNUALLY\",
            \"BudgetLimit\": {\"Amount\": \"100\", \"Unit\": \"USD\"},
            \"CostTypes\": {\"IncludeCredit\": false, \"IncludeRefund\": false}}" \
            --notifications-with-subscribers "[${alerts}]"
    fi

    # At $90 the instance is stopped, not terminated, so its disk and recordings survive.
    local role=touchstone-budget-action existing
    ensure_role "$role" budgets.amazonaws.com
    aws iam attach-role-policy --role-name "$role" \
        --policy-arn arn:aws:iam::aws:policy/AWSBudgetsActions_RolePolicyForResourceAdministrationWithSSM
    existing=$(aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET_NAME" \
        --query "Actions[?contains(to_string(Definition.SsmActionDefinition.InstanceIds), '${INSTANCE_ID}')].ActionId" --output text)
    if [ -z "$existing" ] || [ "$existing" = None ]; then
        local attempt
        # A new role takes a few seconds before Budgets can use it.
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            aws budgets create-budget-action --account-id "$ACCOUNT" --budget-name "$BUDGET_NAME" \
                --notification-type ACTUAL --action-type RUN_SSM_DOCUMENTS \
                --action-threshold ActionThresholdValue=90,ActionThresholdType=ABSOLUTE_VALUE \
                --definition "{\"SsmActionDefinition\":{\"ActionSubType\":\"STOP_EC2_INSTANCES\",\"Region\":\"${AWS_REGION}\",\"InstanceIds\":[\"${INSTANCE_ID}\"]}}" \
                --execution-role-arn "$(aws iam get-role --role-name "$role" --query Role.Arn --output text)" \
                --approval-model AUTOMATIC \
                --subscribers "SubscriptionType=EMAIL,Address=${BUDGET_EMAIL}" >/dev/null && break
            [ "$attempt" = 10 ] && die "could not create the budget action"
            sleep "${RETRY_DELAY_S:-10}"
        done
    fi
}

command -v aws >/dev/null || die "the AWS CLI is not installed"
[ -n "${BUDGET_EMAIL:-}" ] || die "set BUDGET_EMAIL in ${DEPLOY_ENV} to the address for budget alerts"
BUDGET_NAME=touchstone
resolve_account
ensure_topic
ensure_bucket
ensure_instance_role
ensure_security_group
ensure_instance
ship_release
wait_for_first_data
ensure_alert
ensure_budget
log "done: instance ${INSTANCE_ID}, bucket ${BUCKET}, region ${AWS_REGION}"
log "subscribe to ntfy topic ${NTFY_TOPIC} on your phone to receive alerts"
