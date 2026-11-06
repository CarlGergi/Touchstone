# Settings and helpers shared by deploy.sh and report.sh.
# shellcheck shell=bash

NAME=touchstone-recorder
DEPLOY_ENV=${DEPLOY_ENV:-deploy.env}

if [ -f "$DEPLOY_ENV" ]; then
    # shellcheck disable=SC1090
    . "$DEPLOY_ENV"
fi

# The project account allows regional services only in us-east-2.
AWS_REGION=${AWS_REGION:-us-east-2}
export AWS_REGION
INSTANCE_TYPE=${INSTANCE_TYPE:-t4g.small}
DISK_GB=${DISK_GB:-50}
PRODUCTS=${PRODUCTS:-BTC-USD,ETH-USD}

die() {
    echo "error: $*" >&2
    exit 1
}

log() {
    echo "==> $*"
}

resolve_account() {
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text) ||
        die "the AWS CLI has no working credentials; run 'aws configure' or 'aws login' first"
    # shellcheck disable=SC2034  # Used by the scripts that source this file.
    BUCKET=touchstone-recordings-${ACCOUNT}
}

find_instance() {
    aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=${NAME}" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[0].Instances[0].InstanceId' --output text
}

# Runs a shell command on the server through Systems Manager, prints its output, and fails if it
# fails. The instance has no inbound ports and no SSH key.
run_remote() {
    local parameters command_id status
    parameters=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["1800"]}))' "$1")
    command_id=$(aws ssm send-command --instance-ids "$INSTANCE_ID" --document-name AWS-RunShellScript \
        --parameters "$parameters" --query Command.CommandId --output text)
    while :; do
        status=$(aws ssm get-command-invocation --command-id "$command_id" --instance-id "$INSTANCE_ID" \
            --query Status --output text 2>/dev/null || echo Pending)
        case $status in
        Pending | InProgress | Delayed) sleep 3 ;;
        *) break ;;
        esac
    done
    aws ssm get-command-invocation --command-id "$command_id" --instance-id "$INSTANCE_ID" \
        --query StandardOutputContent --output text
    if [ "$status" != Success ]; then
        aws ssm get-command-invocation --command-id "$command_id" --instance-id "$INSTANCE_ID" \
            --query StandardErrorContent --output text >&2
        die "remote command ended with status ${status}"
    fi
}
