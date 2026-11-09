"""Release-local identity shared by logs, migrations and operational snapshots."""
import re

from . import config


def current() -> str:
    # Never fall back to a host's unrelated top-level marker or a mutable env
    # override: each admitted process stays bound to its immutable release root.
    try:
        with (config.ROOT / '.version').open('r', encoding='ascii') as source:
            value = source.read(130).strip()
    except (OSError, UnicodeError):
        return 'unknown'
    return value if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}', value) else 'unknown'
