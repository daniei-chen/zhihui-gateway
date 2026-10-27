#!/usr/bin/env bash
# K28: keep the kernel lock for the entire child command.
set -eu
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/release_lock.py" "$@"
