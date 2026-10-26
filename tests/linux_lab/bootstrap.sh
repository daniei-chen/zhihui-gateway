#!/usr/bin/env bash
# LOCAL disposable VM only. These checks prevent accidental use on production.
set -euo pipefail
test "$(hostname)" = 'zhihui-local-lab'
test "$(id -un)" = 'gateway-lab'
test -f /var/tmp/zhihui-lab-ready
export TZ=Asia/Shanghai
sudo env DEBIAN_FRONTEND=noninteractive apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3-venv nginx git openssl curl ca-certificates libatomic1
mkdir -p /home/gateway-lab/workspace
python3 -m venv /home/gateway-lab/venv
printf 'LOCAL_LAB_BOOTSTRAP_READY\n'
