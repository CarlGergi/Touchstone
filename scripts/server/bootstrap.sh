#!/usr/bin/env bash
# Runs once, as the new instance's user data. Prepares a bare Amazon Linux 2023 arm64 host.
set -euo pipefail

dnf install -y docker
systemctl enable --now docker

# A cushion for a 2 GB instance. A full day's merge peaked at about 700 MB when measured.
dd if=/dev/zero of=/swapfile bs=1M count=2048
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap defaults 0 0' >>/etc/fstab

# The container runs as uid 1000.
mkdir -p /var/lib/touchstone/data /opt/touchstone /etc/touchstone
chown -R 1000:1000 /var/lib/touchstone/data
