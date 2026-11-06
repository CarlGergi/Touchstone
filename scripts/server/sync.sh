#!/usr/bin/env bash
# Copies finished recordings to S3 once a day: merged daily Parquet files and health logs, which
# hold the round-trip samples. Files still being written live under .spool and are left out.
# It never deletes anything from the bucket, and alerts if it fails.
#
# Yesterday's summary goes up last, so its arrival proves the sync finished. The alert function
# turns it into the daily push. It runs in its own container with a memory limit, so that it cannot
# take the recorder down.
set -euo pipefail

notify_failure() {
    curl --silent --max-time 10 --header "Title: Recorder S3 sync failed" --header "Priority: high" \
        --data "The daily sync to ${TOUCHSTONE_BUCKET} failed. Recordings are still on the server." \
        "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null || true
}
trap notify_failure ERR

aws s3 sync "${DATA_DIR}" "s3://${TOUCHSTONE_BUCKET}/recordings" \
    --exclude '*' \
    --include '*/20??-??-??.parquet' \
    --include 'health/*.jsonl' \
    --exclude '.spool/*' \
    --only-show-errors

yesterday=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(days=1)).strftime("%Y-%m-%d"))')
docker run --rm --memory=1g --env POLARS_MAX_THREADS=2 --volume "${DATA_DIR}:/data" \
    touchstone-recorder:current \
    python -m touchstone.recorder daily-summary "$yesterday" \
    --data-dir /data --products "$TOUCHSTONE_PRODUCTS" |
    aws s3 cp - "s3://${TOUCHSTONE_BUCKET}/ops/daily/${yesterday}.json" --only-show-errors
