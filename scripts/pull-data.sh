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

# Options: --from DAY --to DAY copies only days in [from, to); --size reports the download and
# stops. The download is refused above 20 GB, well inside S3's free 100 GB a month.
FROM='' TO='' SIZE_ONLY=''
while [ $# -gt 0 ]; do
    case $1 in
    --from) FROM=$2 && shift 2 ;;
    --to) TO=$2 && shift 2 ;;
    --size) SIZE_ONLY=1 && shift ;;
    *) die "unknown option: $1" ;;
    esac
done
range=()
[ -z "$FROM" ] || range+=(--from "$FROM")
[ -z "$TO" ] || range+=(--to "$TO")

resolve_account
listing=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix recordings/ \
    --query 'Contents[].[Key,Size]' --output text)
size=$(printf '%s\n' "$listing" | uv run --quiet python -m touchstone.pull plan --dest data-server ${range[@]+"${range[@]}"})
log "to download${FROM:+ from $FROM}${TO:+ up to $TO}: ${size}"
if [ -n "$SIZE_ONLY" ]; then
    trap - EXIT
    exit 0
fi
filters=()
while IFS= read -r line; do
    [ -z "$line" ] || filters+=("$line")
done < <(uv run --quiet python -m touchstone.pull filters ${range[@]+"${range[@]}"})
aws s3 sync "s3://${BUCKET}/recordings/" data-server/ --only-show-errors ${filters[@]+"${filters[@]}"}
aws s3 sync "s3://${BUCKET}/latency-profiles/" data-server/latency-profiles/ --only-show-errors
log "data-server/ now holds $(find data-server -name '20??-??-??.parquet' | wc -l | tr -d ' ') daily file(s)"
