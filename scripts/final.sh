#!/usr/bin/env bash
# The held-out evaluation, start to finish, run once after the 2026-11-15 00:30 UTC sync:
# copies only 2026-11-12 to 2026-11-14 from S3, checks that every held-out day arrived, runs the
# one-time evaluation, then redraws the figures and writes the numbers into README.md.
# Nothing is committed or deleted.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

DATA_DIR=${DATA_DIR:-data-server}
DAYS=(2026-11-12 2026-11-13 2026-11-14)
HELD_OUT_PRODUCTS=(BTC-USD ETH-USD)

scripts/pull-data.sh --from "${DAYS[0]}" --to 2026-11-15

# The evaluation can run only once, so a day missing now would be missing for good.
missing=()
for product in "${HELD_OUT_PRODUCTS[@]}"; do
    for day in "${DAYS[@]}"; do
        [ -s "${DATA_DIR}/${product}/${day}.parquet" ] || missing+=("${product} ${day}")
    done
done
if [ ${#missing[@]} -gt 0 ]; then
    die "held-out days not copied yet, so the evaluation was not started: ${missing[*]}. The server copies each day at 00:30 UTC the next day."
fi

log "running the one-time held-out evaluation"
uv run python -m touchstone.experiments.holdout --final --data-dir "$DATA_DIR"
uv run python -m touchstone.experiments.figures
uv run python -m touchstone.experiments.readme
log "done: check README.md and docs/images, then commit"
