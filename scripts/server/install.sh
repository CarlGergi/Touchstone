#!/usr/bin/env bash
# Installs or updates the recorder on the server. Takes the image tag to run.
# Expects the settings in /etc/touchstone/env, which deploy.sh writes.
set -euo pipefail

tag=$1
set -a
# shellcheck disable=SC1091
. /etc/touchstone/env
set +a

aws s3 sync "s3://${TOUCHSTONE_BUCKET}/deploy/server/" /opt/touchstone/ --only-show-errors
chmod +x /opt/touchstone/*.sh /opt/touchstone/*.py

aws s3 cp "s3://${TOUCHSTONE_BUCKET}/deploy/recorder-${tag}.tar.gz" - | gunzip | docker load
docker tag "touchstone-recorder:${tag}" touchstone-recorder:current

cp /opt/touchstone/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable touchstone-recorder.service touchstone-sync.timer touchstone-heartbeat.timer
systemctl restart touchstone-recorder.service
systemctl start touchstone-sync.timer touchstone-heartbeat.timer
