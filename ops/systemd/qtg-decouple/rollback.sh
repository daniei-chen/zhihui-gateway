#!/usr/bin/env bash
# K13 rollback: restore the backed-up unit file (default), daemon-reload, restart.
# Removes the drop-in instead when USE_DROPIN=1 (only for environments where that mode is used).
set -euo pipefail

UNIT_NAME="${UNIT_NAME:-qoder2-engine.service}"
QT_UNIT_NAME="${QT_UNIT_NAME:-qtg.service}"
DROPIN_NAME="${DROPIN_NAME:-10-qtg-decouple.conf}"
USE_DROPIN="${USE_DROPIN:-0}"
BACKUP_DIR="${BACKUP_DIR:-}"
DROPIN_DIR="${DROPIN_DIR:-/etc/systemd/system/${UNIT_NAME}.d}"
DROPIN_PATH="${DROPIN_DIR}/${DROPIN_NAME}"

log() { printf '[k13-rollback] %s\n' "$*"; }
die() { printf '[k13-rollback] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "must run as root"

expected_partof=""
[ -n "${BACKUP_DIR:-}" ] && [ -f "$BACKUP_DIR/metadata.json" ] && \
  expected_partof="$(sed -n 's/.*"partof_before":"\([^"]*\)".*/\1/p' "$BACKUP_DIR/metadata.json")"
fragment="$(systemctl show -p FragmentPath --value "$UNIT_NAME" 2>/dev/null || true)"

if [ "$USE_DROPIN" = "1" ]; then
  if [ -f "$DROPIN_PATH" ]; then
    rm -f "$DROPIN_PATH"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    log "removed drop-in ${DROPIN_PATH}"
  else
    log "drop-in already absent: ${DROPIN_PATH}"
  fi
else
  [ -n "${BACKUP_DIR:-}" ] || die "BACKUP_DIR is required for direct-edit rollback"
  restore="${BACKUP_DIR}/$(basename "${fragment:-$UNIT_NAME}")"
  [ -f "$restore" ] || die "backup unit not found: ${restore}"
  cp -a "$restore" "$fragment"
  log "restored ${fragment} from ${restore}"
fi

systemctl daemon-reload
systemctl restart "$UNIT_NAME"
systemctl is-active --quiet "$UNIT_NAME" || die "${UNIT_NAME} not active after rollback"

partof_now="$(systemctl show -p PartOf --value "$UNIT_NAME" 2>/dev/null || true)"
ok=true
if [ -n "$expected_partof" ]; then
  if [[ "$partof_now" != *"$expected_partof"* ]]; then
    log "PartOf mismatch: expected to contain '${expected_partof}', got '${partof_now}'"
    ok=false
  fi
else
  log "no backup metadata; current PartOf='${partof_now}' (not asserted)"
fi

printf '{"ok":%s,"unit":"%s","qt_unit":"%s","partof_expected":"%s","partof_now":"%s","backup":"%s"}\n' \
  "$ok" "$UNIT_NAME" "$QT_UNIT_NAME" "$expected_partof" "$partof_now" "$BACKUP_DIR"
[ "$ok" = "true" ] || exit 1
log "done"
