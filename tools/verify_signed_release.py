#!/usr/bin/env python3
"""Retired compatibility entrypoint; never approves or reads a release."""


def main() -> int:
    print('RETIRED: use the verified deployment controller.')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
