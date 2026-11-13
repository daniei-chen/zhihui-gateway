#!/usr/bin/env python3
"""Retired compatibility entrypoint.

Signed release approval is owned by the current managed deployment controller.
Keep this name as a fail-closed, side-effect-free stub so old automation cannot
silently fall back to the retired verifier.
"""


def main() -> int:
    print('RETIRED: use the verified deployment controller.')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
