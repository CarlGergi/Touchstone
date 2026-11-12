#!/usr/bin/env bash
# Ends the recording, after the held-out window has closed. It stops the recorder, merges and
# syncs the last files, confirms that every file on the server is in S3 at the same size, and only
# then deletes the instance, its disk, the alert function and the budget's stop action. The bucket
# and the budget's alert emails are kept.
#
#   scripts/teardown.sh           refuses before 2026-11-15 00:00 UTC, then asks for confirmation
#   scripts/teardown.sh --check   rehearses the checks and the permissions, and changes nothing
#   scripts/teardown.sh --early   allows running before that date, for abandoning the recording
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

EARLIEST_EPOCH=1794700800 # 2026-11-15 00:00 UTC, when the held-out window closes
ALERT=touchstone-alert
BUDGET_NAME=touchstone

mode=${1:-delete}
case $mode in
delete | --check | --early) ;;
*) die "unknown option ${mode}" ;;
esac

if [ "$mode" = delete ] && [ "$(date -u +%s)" -lt "$EARLIEST_EPOCH" ]; then
    die "the held-out window runs to 2026-11-15 00:00 UTC, so this refuses to run before then. Use --early only to abandon the recording."
fi

resolve_account
INSTANCE_ID=$(find_instance)
[ "$INSTANCE_ID" != None ] || die "no recorder instance found in ${AWS_REGION}"
volumes=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].BlockDeviceMappings[].Ebs.VolumeId' --output text)
[ -n "$volumes" ] && [ "$volumes" != None ] || die "could not find the instance's disk"

budget_actions() {
    aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET_NAME" \
        --query 'Actions[].ActionId' --output text 2>/dev/null || true
}

# Compares the server's finished files with S3, by name and size. The first argument says whether
# a difference stops the teardown ("strict") or is only reported ("report").
compare_with_s3() {
    local server listing
    server=$(run_remote "cd /var/lib/touchstone/data && echo spool-parts \$(find .spool -name 'part-*.parquet' 2>/dev/null | wc -l) && find . -path ./.spool -prune -o -type f \\( -name '20??-??-??.parquet' -o -name '20??-??-??.jsonl' \\) -printf '%P %s\\n'")
    listing=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix recordings/ \
        --query 'Contents[].[Key,Size]' --output text)
    SERVER=$server LISTING=$listing MODE=$1 python3 - <<'PYTHON'
import os
import sys

parts = 0
server = {}
for line in os.environ["SERVER"].splitlines():
    fields = line.split()
    if len(fields) != 2:
        continue
    if fields[0] == "spool-parts":
        parts = int(fields[1])
    else:
        server[fields[0]] = int(fields[1])
stored = {}
for line in os.environ["LISTING"].splitlines():
    fields = line.split()
    if len(fields) == 2 and fields[0].startswith("recordings/"):
        stored[fields[0].removeprefix("recordings/")] = int(fields[1])

problems = [f"{name}: {size} bytes on the server, {stored.get(name)} in S3"
            for name, size in sorted(server.items()) if stored.get(name) != size]
if parts:
    problems.append(f"{parts} part file(s) not merged yet")
print(f"{len(server)} finished file(s) on the server, {len(stored)} in S3, {len(problems)} difference(s)")
for problem in problems:
    print(f"  {problem}")
if os.environ["MODE"] == "strict" and (problems or not server):
    sys.exit(1)
PYTHON
}

if [ "$mode" = --check ]; then
    log "rehearsal: nothing will be changed"
    log "would delete instance ${INSTANCE_ID}, disk(s) ${volumes}, function ${ALERT} and budget action(s) $(budget_actions); would keep s3://${BUCKET}"
    compare_with_s3 report
    for attempt in "terminate-instances --instance-ids ${INSTANCE_ID}" "delete-volume --volume-id ${volumes%% *}"; do
        # AWS answers a permitted dry run with an error named DryRunOperation.
        # shellcheck disable=SC2086  # Deliberately split into arguments.
        output=$(aws ec2 $attempt --dry-run 2>&1 || true)
        case $output in
        *DryRunOperation*) log "permission confirmed: ${attempt%% *}" ;;
        *) die "no permission for ${attempt%% *}: ${output}" ;;
        esac
    done
    exit 0
fi

echo "This stops recording and permanently deletes instance ${INSTANCE_ID}, disk(s) ${volumes},"
echo "the alert function and the budget's stop action,"
echo "after confirming that every finished file is in s3://${BUCKET}, which is kept."
printf 'Type "delete" to continue: '
read -r answer
[ "$answer" = delete ] || die "cancelled, nothing was changed"

# The alert goes first, so that stopping the recorder does not raise it.
log "removing the alert function"
aws events remove-targets --rule "$ALERT" --ids 1 >/dev/null
aws events delete-rule --name "$ALERT"
aws lambda delete-function --function-name "$ALERT"

log "stopping the recorder, merging the last files and syncing them"
run_remote "set -e
systemctl stop touchstone-heartbeat.timer touchstone-sync.timer touchstone-recorder.service
set -a; . /etc/touchstone/env; set +a
docker run --rm --env POLARS_MAX_THREADS=2 --volume \"\${DATA_DIR}:/data\" touchstone-recorder:current python -m touchstone.recorder compact --data-dir /data --include-today
/opt/touchstone/sync.sh"

log "confirming that everything reached S3"
compare_with_s3 strict ||
    die "S3 does not match the server, so nothing was deleted. The recorder is stopped and alerts are off: fix the sync and run this again, or run make deploy to resume."

log "deleting the instance and its disk"
aws ec2 modify-instance-attribute --instance-id "$INSTANCE_ID" --no-disable-api-termination
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null
aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
for volume in $volumes; do
    aws ec2 wait volume-available --volume-ids "$volume"
    aws ec2 delete-volume --volume-id "$volume"
done

# The stop action pointed at the instance just deleted. The budget's alert emails are kept.
log "deleting the budget's stop action"
for action in $(budget_actions); do
    [ "$action" = None ] && continue
    aws budgets delete-budget-action --account-id "$ACCOUNT" --budget-name "$BUDGET_NAME" --action-id "$action" >/dev/null
done
log "done: instance, disk, alert and stop action deleted, recordings kept in s3://${BUCKET}"
