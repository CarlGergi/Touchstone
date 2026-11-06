#!/usr/bin/env bash
# Saves the Mac's health logs to S3 as the "home" latency profile. They are the only record of
# round trips from a home connection, so they are kept apart from the server's.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

resolve_account
[ -d data/health ] || die "no health logs in data/health"
aws s3 sync data/health "s3://${BUCKET}/latency-profiles/home/health/" \
    --exclude '*' --include '*.jsonl' --only-show-errors
log "saved $(find data/health -name '*.jsonl' | wc -l | tr -d ' ') health log(s) to s3://${BUCKET}/latency-profiles/home/health/"
