"""npm audit 的豁免闸门 —— 唯一判定者。

## 为什么需要它

`frontend` 是必检之一，而它此前直接跑 `npm audit --audit-level=high`：上游发一条
high 通告，这个必检就整块挡死，所有 PR 一起卡住，只能等上游修。反过来，简单加一条
`|| true` 更糟：那会把「有漏洞」「网络失败」「输出半截」一起变成绿。

所以职责拆开：

* workflow 里只对 **`npm audit --json > audit.json` 这一条命令** 放行（`|| true`），
  install / lint / typecheck / build 任何一步都不许放行；
* 本脚本是**唯一判定者**：先要求报告可解析且含 `metadata.vulnerabilities`
  （否则一次网络失败会被当成「没有漏洞」），再要求每个 high/critical 都有
  **未过期**的豁免条目。

## 豁免条目

```json
{"schema": 1, "exemptions": [
  {"package": "lodash", "advisory": "GHSA-xxxx-xxxx-xxxx", "severity": "high",
   "expires_on": "2026-12-31", "mitigation": "只在内网离线脚本里调用，输入不受外部控制"}
]}
```

`advisory` 支持 `*` 表示该包的全部通告。**没有 `expires_on` 的豁免等于永久后门，不接受。**
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import sys

HIGH_SEVERITIES = ('high', 'critical')


class AuditReportError(Exception):
    """报告本身不成立 —— 不能当作「没有漏洞」。"""


def load_report(path: Path) -> dict:
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise AuditReportError(f'读不到审计报告（{path}）：{exc}') from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AuditReportError(f'审计报告不是合法 JSON（{path}）：{exc}') from exc
    if not isinstance(data, dict):
        raise AuditReportError('审计报告的顶层不是对象')
    metadata = data.get('metadata')
    if not isinstance(metadata, dict) or 'vulnerabilities' not in metadata:
        raise AuditReportError(
            '审计报告缺少 metadata.vulnerabilities —— 这不是 `npm audit --json` 的输出，'
            '不能据此认为没有漏洞')
    return data


def high_findings(report: dict) -> list:
    """把 high/critical 的通告摊平成 (包名, 通告 id 集合, severity)。"""
    findings = []
    for name, item in sorted((report.get('vulnerabilities') or {}).items()):
        if not isinstance(item, dict):
            continue
        severity = str(item.get('severity') or '').lower()
        if severity not in HIGH_SEVERITIES:
            continue
        advisories = set()
        for via in item.get('via') or []:
            if isinstance(via, dict):
                for key in ('source', 'url', 'title', 'name'):
                    if via.get(key):
                        advisories.add(str(via[key]))
        findings.append({'package': name, 'advisories': advisories, 'severity': severity})
    return findings


def _entry_covers(entry: dict, finding: dict) -> bool:
    if entry.get('package') != finding['package']:
        return False
    advisory = str(entry.get('advisory') or '')
    return advisory == '*' or advisory in finding['advisories']


def _entry_problem(entry: dict, today: datetime.date) -> str:
    """返回该条目为什么不能用于豁免；空字符串表示可用。"""
    if not str(entry.get('mitigation') or '').strip():
        return '缺少 mitigation（缓解措施）'
    raw = str(entry.get('expires_on') or '').strip()
    if not raw:
        return '缺少 expires_on —— 没有到期日的豁免是永久后门'
    try:
        expires = datetime.date.fromisoformat(raw)
    except ValueError:
        return f'expires_on 不是 YYYY-MM-DD：{raw!r}'
    if expires < today:
        return f'豁免已于 {raw} 到期'
    return ''


def evaluate(report: dict, exemptions: dict, today: datetime.date) -> dict:
    """返回 {'failures': [...], 'covered': [...], 'waived': [...]}。"""
    findings = high_findings(report)
    entries = exemptions.get('exemptions') or []
    if not isinstance(entries, list):
        raise AuditReportError('豁免文件的 exemptions 必须是数组')
    failures, covered = [], []
    for finding in findings:
        usable = []
        for entry in entries:
            if not isinstance(entry, dict) or not _entry_covers(entry, finding):
                continue
            problem = _entry_problem(entry, today)
            if problem:
                failures.append(f'{finding["package"]}（{finding["severity"]}）：'
                                f'豁免不可用 —— {problem}')
            else:
                usable.append(entry)
        if usable:
            covered.append(f'{finding["package"]}（{finding["severity"]}）由豁免放行至 '
                           f'{sorted(str(e["expires_on"]) for e in usable)[0]}：'
                           f'{usable[0]["mitigation"]}')
        elif not any(f.startswith(finding['package'] + '（') for f in failures):
            ids = ', '.join(sorted(finding['advisories'])) or '(无通告 id)'
            failures.append(f'{finding["package"]}（{finding["severity"]}）：'
                            f'没有未到期的豁免，通告 {ids}')
    return {'failures': failures, 'covered': covered, 'findings': len(findings)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='npm audit 豁免闸门')
    parser.add_argument('--report', required=True, help='npm audit --json 的输出文件')
    parser.add_argument('--exemptions', required=True, help='豁免清单 JSON')
    parser.add_argument('--today', help='覆盖当天日期（测试用，YYYY-MM-DD）')
    args = parser.parse_args(argv)

    today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    try:
        report = load_report(Path(args.report))
        exemptions = json.loads(Path(args.exemptions).read_text(encoding='utf-8'))
    except AuditReportError as exc:
        print(f'✗ {exc}', file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f'✗ 豁免清单不可解析（{args.exemptions}）：{exc}', file=sys.stderr)
        return 2

    result = evaluate(report, exemptions, today)
    for line in result['covered']:
        print(f'  已签豁免：{line}')
    if result['failures']:
        print(f'✗ {len(result["failures"])} 条 high/critical 依赖漏洞未被有效豁免：',
              file=sys.stderr)
        for line in result['failures']:
            print(f'  - {line}', file=sys.stderr)
        print('  处置：升级依赖；确需豁免就写进工具清单并带 expires_on 与缓解措施。',
              file=sys.stderr)
        return 1
    print(f'依赖审计通过：{result["findings"]} 条 high/critical，全部在有效豁免内'
          if result['findings'] else '依赖审计通过：没有 high/critical 通告')
    return 0


if __name__ == '__main__':
    sys.exit(main())
