#!/usr/bin/env bash
# Copies finished recordings and both latency profiles from S3 to data-server/ on this machine, so
# the data never lives only in AWS. Pushes an alert if it fails, for example when the AWS sign-in
# has expired, so a scheduled run cannot fail silently.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

notify_failure() {
    curl --silent --max-time 10 --header "Title: Touchstone pull-data failed" --header "Priority: high" \
        --data "Copying recordings from S3 to the Mac failed. Run aws login, then make pull-data." \
        "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null || true
}
# An exit trap, because a failed sign-in ends the script through exit, which ERR does not catch.
trap 'status=$?; [ "$status" -eq 0 ] || notify_failure' EXIT

resolve_account
aws s3 sync "s3://${BUCKET}/recordings/" data-server/ --only-show-errors
aws s3 sync "s3://${BUCKET}/latency-profiles/" data-server/latency-profiles/ --only-show-errors
log "data-server/ now holds $(find data-server -name '20??-??-??.parquet' | wc -l | tr -d ' ') daily file(s)"
