#!/usr/bin/env bash
# Prints the server's health summary, then checks each finished day from its S3 copy, which tests
# the sync as well. verify loads a whole day into memory, so it runs here and not on the server.
# Usage: scripts/report.sh [first-day], where first-day is a UTC day like 2026-11-05.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

since=${1:-}
resolve_account
INSTANCE_ID=$(find_instance)
[ "$INSTANCE_ID" != None ] || die "no recorder instance found in ${AWS_REGION}"

log "server status"
run_remote "systemctl is-active touchstone-recorder.service touchstone-sync.timer touchstone-heartbeat.timer; systemctl show touchstone-recorder.service --property=NRestarts,ActiveEnterTimestamp; df -h /var/lib/touchstone/data | tail -1; du -sh /var/lib/touchstone/data; free -m | head -2"

log "health summary"
run_remote "docker exec touchstone-recorder python -m touchstone.recorder report --data-dir /data ${since:+--since ${since}}"

log "last write times"
aws s3 cp "s3://${BUCKET}/ops/heartbeat.json" - 2>/dev/null || echo "no heartbeat yet"
echo

log "checking finished days from S3"
aws s3 sync "s3://${BUCKET}/recordings/" data-server/ --only-show-errors
aws s3 sync "s3://${BUCKET}/latency-profiles/" data-server/latency-profiles/ --only-show-errors
for product in ${PRODUCTS//,/ }; do
    for path in "data-server/${product}"/*.parquet; do
        [ -e "$path" ] || continue
        day=$(basename "$path" .parquet)
        if [ -n "$since" ] && [[ "$day" < "$since" ]]; then
            continue
        fi
        echo "${product} ${day}: $(uv run python -m touchstone.recorder verify "$product" "$day" --data-dir data-server | tr -d '\n ')"
    done
done

log "latency profiles"
uv run python -m touchstone.latency summary --data-dir data-server
