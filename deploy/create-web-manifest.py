#!/usr/bin/env python3
"""Build and sign the static ``web/out`` closure with Ed25519."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            value.update(chunk)
    return value.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        raise SystemExit("usage: create-web-manifest.py WEB_OUT [SIGNING_KEY]")
    root = Path(argv[0]).resolve(strict=True)
    key = Path(argv[1]) if len(argv) == 2 else Path("/opt/zhihui-release/keys/release-priv.pem")
    if not root.is_dir() or root.is_symlink() or not key.is_file():
        raise ValueError("web output and signing key are required")
    assets = []
    for path in sorted(root.rglob("*")):
        if path.name in {"manifest.sha256", "manifest.sig"}:
            continue
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe web output entry: " + str(path))
        assets.append(path)
    names = [path.relative_to(root).as_posix() for path in assets]
    if not {"index.html", "login/index.html"}.issubset(names):
        raise ValueError("required signed web entries are missing")
    content = "".join("%s  %d  %s\n" % (digest(path), path.stat().st_size,
                                          path.relative_to(root).as_posix())
                       for path in assets).encode()
    with tempfile.TemporaryDirectory(prefix=".web-sign-", dir=root.parent) as name:
        stage = Path(name)
        manifest = stage / "manifest.sha256"
        signature = stage / "manifest.sig"
        manifest.write_bytes(content)
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                        "-in", str(manifest), "-out", str(signature)], check=True,
                       capture_output=True, timeout=60)
        subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey",
                        "/opt/workbuddy-manager/release-trust-ed25519.pem", "-rawin",
                        "-in", str(manifest), "-sigfile", str(signature)], check=True,
                       capture_output=True, timeout=30)
        signature.chmod(0o644)
        for source, target in ((manifest, root / "manifest.sha256"),
                              (signature, root / "manifest.sig")):
            os.replace(source, target)
    print("signed web files=%d root=%s" % (len(assets), root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
