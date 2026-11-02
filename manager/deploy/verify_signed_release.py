"""Retired legacy verifier. Structure and checksums cannot approve a publisher.

The repository tools/ entry is retained for historical callers; neither entry
reads or extracts an archive. Only the independently trusted controller may
approve a release.
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
