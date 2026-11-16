#!/bin/bash
# Install the memory/task ceiling for the one-shot rolling helper.
# This does not start or restart the helper and does not touch the primary unit.
set -euo pipefail

DROPIN=/etc/systemd/system/workbuddy-web-helper.service.d/60-resource-budget.conf
[ "$(id -u)" = 0 ] || { echo "resource-budget: root required" >&2; exit 2; }
[ -d /etc/systemd/system ] || { echo "resource-budget: systemd directory missing" >&2; exit 2; }

mkdir -p "$(dirname "$DROPIN")"
tmp=$(mktemp "${DROPIN}.XXXXXX")
trap 'rm -f "$tmp"' EXIT
cat >"$tmp" <<'EOF'
[Service]
MemoryHigh=1G
MemoryMax=1280M
MemorySwapMax=256M
TasksMax=512
EOF
chmod 0644 "$tmp"
chown 0:0 "$tmp"
if [ -f "$DROPIN" ] && cmp -s "$tmp" "$DROPIN"; then
    :
else
    mv -f "$tmp" "$DROPIN"
fi
trap - EXIT
systemctl daemon-reload

high=$(systemctl show workbuddy-web-helper.service -p MemoryHigh --value)
maximum=$(systemctl show workbuddy-web-helper.service -p MemoryMax --value)
swap=$(systemctl show workbuddy-web-helper.service -p MemorySwapMax --value)
tasks=$(systemctl show workbuddy-web-helper.service -p TasksMax --value)
[ "$high" = "1073741824" ] || { echo "resource-budget: MemoryHigh=$high" >&2; exit 1; }
[ "$maximum" = "1342177280" ] || { echo "resource-budget: MemoryMax=$maximum" >&2; exit 1; }
[ "$swap" = "268435456" ] || { echo "resource-budget: MemorySwapMax=$swap" >&2; exit 1; }
[ "$tasks" = "512" ] || { echo "resource-budget: TasksMax=$tasks" >&2; exit 1; }
echo "resource-budget: helper MemoryHigh=1G MemoryMax=1280M MemorySwapMax=256M TasksMax=512"
