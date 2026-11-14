#!/bin/sh
# Prepare a staged release for the root-owned writer-guard.
#
# Public GitHub artifacts deliberately do not contain the production private
# key or host-specific detached signatures.  Run this command after extracting
# a release on the production host and before migration_entry.py prepare.
set -eu

die() {
  echo "managed-stage: $*" >&2
  exit 2
}

[ "$(id -u)" = 0 ] || die "must run as root"
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || die "usage: $0 RELEASE_DIR [SIGNING_KEY]"

root=$1
key=${2:-/opt/zhihui-release/keys/release-priv.pem}
python=${WB_SIGN_PYTHON:-/usr/bin/python3}
cache=${WB_PYCACHE_PREFIX:-/opt/zhihui-release/python-cache}
trust=${WB_RELEASE_TRUST:-/opt/workbuddy-manager/release-trust-ed25519.pem}

case "$root" in
  /*) ;;
  *) die "release directory must be absolute" ;;
esac
[ -d "$root" ] || die "release directory is missing"
[ -f "$root/.version" ] || die "release version marker is missing"
[ -f "$root/deploy/create-web-manifest.py" ] || die "web signer is missing"
[ -f "$root/deploy/create-managed-release.py" ] || die "managed-release builder is missing"
[ -f "$root/deploy/sign-release-ed25519.py" ] || die "release signer is missing"
[ -f "$key" ] || die "signing key is missing"
[ -f "$trust" ] || die "release trust anchor is missing"

key_owner=$(stat -c '%u:%g' "$key")
key_mode=$(stat -c '%a' "$key")
[ "$key_owner" = 0:0 ] || die "signing key must be root-owned"
[ "$key_mode" = 600 ] || die "signing key must be mode 600"

"$python" -I -S -B -X pycache_prefix="$cache" \
  "$root/deploy/create-web-manifest.py" "$root/web/out" "$key"
"$python" -I -S -B -X pycache_prefix="$cache" \
  "$root/deploy/create-managed-release.py" "$root"
WB_SIGN_KEY="$key" "$python" -I -S -B -X pycache_prefix="$cache" \
  "$root/deploy/sign-release-ed25519.py" --rebuild \
  --manifest "$root/deploy/managed-release.json" --key "$key" --trust "$trust"

# Verify the two host-side closures before the writer controller sees them.
"$python" -I -S -B -X pycache_prefix="$cache" \
  "$root/deploy/web_preflight.py" --root "$root/web/out" --quiet
preflight=${WB_TRUSTED_PREFLIGHT:-/opt/zhihui-release/bin/preflight_candidate.py}
[ -f "$preflight" ] || die "trusted preflight is missing"
"$preflight" "$root"

version=$(cat "$root/.version")
echo "MANAGED-STAGE-READY version=$version root=$root"
