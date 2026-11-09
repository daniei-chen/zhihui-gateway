from __future__ import annotations

import json
from pathlib import Path


def contracts():
    value = json.loads(Path(__file__).with_name('protocol-contracts.json').read_text(encoding='utf-8'))
    if value.get('schema') != 1 or value.get('product') != 'zhihui-gateway':
        raise ValueError('invalid protocol contract')
    return value


def compare(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError('object contracts required')
    for value in (before, after):
        if value.get('schema') != 1 or value.get('product') != 'zhihui-gateway' or not isinstance(value.get('protocols'), dict):
            raise ValueError('unsupported protocol contract schema')
        for protocol in value['protocols'].values():
            if (not isinstance(protocol, dict) or not isinstance(protocol.get('path'), str)
                    or not isinstance(protocol.get('capabilities'), dict)
                    or any(item is not None and type(item) is not bool for item in protocol['capabilities'].values())):
                raise ValueError('invalid protocol capability record')
    changes = []
    for name, previous in before.get('protocols', {}).items():
        current = after.get('protocols', {}).get(name)
        if current is None:
            changes.append({'protocol': name, 'change': 'protocol_removed', 'breaking': True})
            continue
        if previous.get('path') != current.get('path'):
            changes.append({'protocol': name, 'change': 'path_changed', 'breaking': True})
        for capability, supported in previous.get('capabilities', {}).items():
            if supported is True and current.get('capabilities', {}).get(capability) is not True:
                changes.append({'protocol': name, 'capability': capability, 'change': 'capability_removed', 'breaking': True})
    return {'scope': 'zhihui-protocol-contract-v1-not-general-openapi',
            'breaking': bool(changes), 'changes': changes}
