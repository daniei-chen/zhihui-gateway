#!/usr/bin/env bash
# Read-only environment discovery. No production services or files are modified.
set -eu
uname -s
command -v python3 || true
command -v docker || true
command -v systemd-analyze || true
free -m
df -h /home/user
ls -ld /home/user/zhihui-execution-20261023 /home/user/zhihui-engineering-20261023 || true
