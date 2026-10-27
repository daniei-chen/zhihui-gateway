#!/usr/bin/env bash
# K13 install: decouple qoder2-engine.service from qtg.service.
# Verified approach (VM rehearsal 2026-10-26): back up the unit file, then remove the
# `PartOf=...qtg...` line(s) from the real unit file.  A drop-in with an empty `PartOf=`
# was tested on systemd 255.4 and did NOT clear the inherited list, so it is not the
# default (kept only as USE_DROPIN=1 for environments that verify it works).
# Idempotent; daemon-reload; enable --now; automatic rollback on failure.
set -euo pipefail

UNIT_NAME="${UNIT_NAME:-qoder2-engine.service}"
QT_UNIT_NAME="${QT_UNIT_NAME:-qtg.service}"
DROPIN_NAME="${DROPIN_NAME:-10-qtg-decouple.conf}"
USE_DROPIN="${USE_DROPIN:-0}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/qtg-decouple/${STAMP}}"
DROPIN_DIR="${DROPIN_DIR:-/etc/systemd/system/${UNIT_NAME}.d}"
DROPIN_PATH="${DROPIN_DIR}/${DROPIN_NAME}"

log() { printf '[k13-install] %s\n' "$*"; }
die() { printf '[k13-install] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "must run as root"

fragment="$(systemctl show -p FragmentPath --value "$UNIT_NAME" 2>/dev/null || true)"
[ -n "${fragment:-}" ] || die "unit not found: ${UNIT_NAME}"
[ -f "$fragment" ] || die "fragment file missing: ${fragment}"

partof_before="$(systemctl show -p PartOf --value "$UNIT_NAME" 2>/dev/null || true)"
mkdir -p "$BACKUP_DIR"
cp -a "$fragment" "$BACKUP_DIR/$(basename "$fragment")"
( cd "$BACKUP_DIR" && sha256sum "$(basename "$fragment")" > "$(basename "$fragment").sha256" )
printf '%s\n' "$partof_before" > "$BACKUP_DIR/PartOf.before"
printf '{"unit":"%s","qt_unit":"%s","fragment":"%s","partof_before":"%s","dropin":"%s","use_dropin":%s,"stamp":"%s"}\n' \
  "$UNIT_NAME" "$QT_UNIT_NAME" "$fragment" "$partof_before" "$DROPIN_PATH" "$USE_DROPIN" "$STAMP" \
  > "$BACKUP_DIR/metadata.json"
log "backup -> ${BACKUP_DIR} (PartOf before: '${partof_before}')"

remove_dropin() { rm -f "$DROPIN_PATH"; }
restore_original() { cp -a "$BACKUP_DIR/$(basename "$fragment")" "$fragment"; }
rollback_on_failure() {
  log "failure -> automatic rollback"
  if [ "$USE_DROPIN" = "1" ]; then remove_dropin; else restore_original; fi
  systemctl daemon-reload || true
  systemctl restart "$UNIT_NAME" 2>/dev/null || true
  log "PartOf after rollback: '$(systemctl show -p PartOf --value "$UNIT_NAME" 2>/dev/null || true)'"
  exit 1
}

changed=0
if [ "$USE_DROPIN" = "1" ]; then
  mkdir -p "$DROPIN_DIR"
  desired=$'[Unit]\nPartOf='
  if [ -f "$DROPIN_PATH" ] && [ "$(cat "$DROPIN_PATH")" = "$desired" ]; then
    log "drop-in already present: ${DROPIN_PATH}"
  else
    printf '%s\n' "$desired" > "${DROPIN_PATH}.tmp"
    mv -f "${DROPIN_PATH}.tmp" "$DROPIN_PATH"
    changed=1
  fi
else
  if grep -qE "^PartOf=.*${QT_UNIT_NAME%%.*}" "$fragment"; then
    sed -i -E "/^PartOf=.*${QT_UNIT_NAME%%.*}/d" "$fragment"
    changed=1
  else
    log "no PartOf=...${QT_UNIT_NAME%%.*} line in ${fragment}; nothing to edit"
  fi
fi
[ "$changed" = "1" ] && systemctl daemon-reload

partof_after="$(systemctl show -p PartOf --value "$UNIT_NAME" 2>/dev/null || true)"
if [[ "$partof_after" == *"$QT_UNIT_NAME"* ]] || [[ "$partof_after" == *"${QT_UNIT_NAME%%.*}"* ]]; then
  log "PartOf still references qtg: '${partof_after}'"
  rollback_on_failure
fi

systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || true
systemctl restart "$UNIT_NAME" || rollback_on_failure
systemctl is-active --quiet "$UNIT_NAME" || rollback_on_failure

printf '{"ok":true,"unit":"%s","qt_unit":"%s","partof_before":"%s","partof_after":"%s","dropin":"%s","backup":"%s","changed":%s}\n' \
  "$UNIT_NAME" "$QT_UNIT_NAME" "$partof_before" "$partof_after" "$DROPIN_PATH" "$BACKUP_DIR" "$changed"
log "done"
