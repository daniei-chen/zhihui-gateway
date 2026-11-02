"""Retired legacy verifier: it did not bind signed text to archive closure.

Do not use this command for release approval. The installed, independently
trusted deployment controller owns publisher identity and complete validation.
This stub deliberately does not read keys, manifests or archives.
"""
from __future__ import annotations


def main() -> int:
    print('status: RETIRED')
    print('Legacy verification is disabled: signed payload, manifest and archive '
          'were not bound together.')
    print('Use the installed trusted deployment controller; this command cannot approve releases.')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
