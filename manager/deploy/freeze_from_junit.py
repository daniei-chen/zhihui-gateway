#!/opt/workbuddy-manager/venv/bin/python
"""从一次完整 junit 报告生成门禁冻结清单（人工复核后使用）。

用法：deploy/freeze_from_junit.py <junit.xml> <deploy/testgate-frozen.json>
"""
from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    junit, target = Path(argv[0]), Path(argv[1])
    root = ET.parse(junit).getroot()
    passed = 0
    failed: list[str] = []
    skipped: list[str] = []
    reasons: dict[str, str] = {}
    for case in root.iter('testcase'):
        ident = '%s::%s' % (case.get('classname', ''), case.get('name', ''))
        if case.find('failure') is not None or case.find('error') is not None:
            failed.append(ident)
        elif case.find('skipped') is not None:
            skipped.append(ident)
            node = case.find('skipped')
            reasons[ident] = (node.get('message') or node.get('type') or 'no reason')[:300]
        else:
            passed += 1
    failed = sorted(set(failed))
    skipped = sorted(set(skipped))
    payload = {
        'schema': 1,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'generator': 'deploy/freeze_from_junit.py',
        'note': 'skipped 集合与 min_passed 是发布门禁的冻结判据；改动必须人工复核。',
        'min_passed': max(1, passed - 5),
        'expected_collected': passed + len(failed) + len(skipped),
        'skipped': skipped,
        'skip_reasons': {ident: reasons.get(ident, 'no reason') for ident in skipped},
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('freeze: passed=%d failed=%d skipped=%d collected=%d -> %s'
          % (passed, len(failed), len(skipped), payload['expected_collected'], target))
    if failed:
        print('WARNING: 报告里有失败，不要用它冻结')
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
