#!/bin/bash
# Offline recovery checks only. Explicit snapshot/archive input is mandatory.
# The Python tool uses a private temporary directory, bounded streaming reads,
# and read-only isolated SQLite files. It never starts an application or reads
# production credentials/configuration. See --help for receipt/key/report paths.
set -euo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON:-python3}" -I "$SCRIPT_DIR/restore_drill.py" "$@"
