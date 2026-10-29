#!/opt/workbuddy-manager/venv/bin/python
"""定位「上游把内容拒绝伪装成 11102」的触发串。

## 背景

上游对特定提示词特征频繁按内容拒绝，但错误体伪装成
`{"code":11102,"msg":"model [x] service info not found"}`，看起来像模型不存在。
2026-10-27 已实锤过一次（同模型同密钥下，裸请求/普通提示词/大体积都 200，
唯独带某特征句的 503），当时的对冲是把那句同义改写进
`settings.prompt_defuse_rules`。现在冒出了新的触发串。

## 这个工具做什么

把候选文本当作 `system` 消息，发一次极小的真实请求（`max_tokens=4`），
看是否被拒；`--bisect` 会按段落切片逐段二分，直接告出「哪一段会触发」。

找到之后**不用发版**，写进设置即可：

    db.set_setting('prompt_defuse_rules', [[触发串, 同义改写], ...])

## 用法

    probe_prompt_trigger.py --text "怀疑的句子"
    probe_prompt_trigger.py --file /tmp/system-prompt.txt --bisect
    probe_prompt_trigger.py --text "..." --dry-run          # 只看要发什么，不发

退出码：0 全部通过；1 有片段触发 11102；2 参数/凭据问题。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROFILE = Path('/home/agentuser/.dsh/profiles/web/cordis.patch.yml')
#: DSH 的凭据库。`cordis.patch.yml` 里只写 `apiKeyEnv: ZAI_CODING_CN_API_KEY` 这种
#: **变量名**，真正的 key 存在这里（`records.llm-pi-ai/<provider>.key`）。
CREDENTIALS = Path('/home/agentuser/.dsh/.credentials.yaml')
DEFAULT_MODEL = 'deepseek-v4.1-flash'
#: 判据与 gateway._is_not_processed_rejection / budget._REJECTED_MARKERS 保持一致
REJECTION_MARKERS = ('no_healthy_account', '"code":11102', '"code": 11102',
                     'model_not_found', 'service info not found')


def _first(pattern: str, text: str) -> str | None:
    # 必须 MULTILINE：下面这些模式都带 ^...$，按行锚定。
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip('\'"') if match else None


def provider_name(profile_text: str) -> str | None:
    """profile 里 `providers:` 下的第一个 provider 名（本机是 `zhihui`）。"""
    lines = profile_text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r'^\s+providers:\s*$', line):
            indent = len(line) - len(line.lstrip())
            for inner in lines[index + 1:]:
                if not inner.strip():
                    continue
                inner_indent = len(inner) - len(inner.lstrip())
                if inner_indent <= indent:
                    break
                name = _first(r'^\s*([\w.\-]+):\s*$', inner)
                if name:
                    return name
    return None


def key_from_store(profile_text: str) -> str | None:
    """从 DSH 凭据库取 `llm-pi-ai/<provider>` 记录里的 `key`。"""
    try:
        text = CREDENTIALS.read_text(errors='replace')
    except OSError:
        return None
    names = []
    provider = provider_name(profile_text)
    if provider:
        names.append('llm-pi-ai/' + provider)
    names.append('llm-pi-ai/')
    current = None
    fallback = None
    for line in text.splitlines():
        record = _first(r'^\s{2}([\w.\-/]+):\s*$', line)
        if record:
            current = record
            continue
        value = _first(r'^\s{4}key:\s*(\S+)\s*$', line)
        if value and current:
            if names and current in names:
                return value
            if current.startswith('llm-pi-ai/') and fallback is None:
                fallback = value
    return fallback


def resolve_credentials(base_url: str | None, api_key: str | None,
                        api_key_env: str | None) -> tuple:
    """按优先级解析 baseURL / apiKey，返回 (base, key)。

    1. `--base-url` / `--api-key` / `--api-key-env`（显式）
    2. profile 的 `baseURL`
    3. `apiKey:` 内联 → `apiKeyEnv:` 指向的环境变量 → DSH 凭据库 `llm-pi-ai/<provider>.key`
    """
    profile_text = ''
    try:
        profile_text = PROFILE.read_text(errors='replace')
    except OSError:
        pass

    base = base_url or _first(r'^\s*baseURL:\s*(\S+)\s*$', profile_text)
    if api_key:
        return base, api_key
    if api_key_env:
        env_value = os.environ.get(api_key_env)
        if env_value:
            return base, env_value
    inline = _first(r'^\s*apiKey:\s*(\S+)\s*$', profile_text)
    if inline:
        return base, inline
    named = _first(r'^\s*apiKeyEnv:\s*(\S+)\s*$', profile_text)
    if named and os.environ.get(named):
        return base, os.environ[named]
    return base, key_from_store(profile_text)


def probe(base_url: str, api_key: str, system_text: str, model: str,
          *, timeout: float = 90.0) -> dict:
    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_text},
            {'role': 'user', 'content': 'ping'},
        ],
        'max_tokens': 4,
        'stream': False,
    }).encode()
    request = urllib.request.Request(
        base_url.rstrip('/') + '/chat/completions', data=payload,
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode('utf-8', 'replace')
            return {'status': response.status, 'rejected': False, 'body': body[:300]}
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode('utf-8', 'replace')
        return {'status': exc.code,
                'rejected': any(marker in body for marker in REJECTION_MARKERS),
                'body': body[:300]}
    except Exception as exc:  # noqa: BLE001
        return {'status': 0, 'rejected': False, 'body': '%s: %s' % (type(exc).__name__, exc)}


def segments(text: str, parts: int) -> list:
    """按行切成若干段；空文本返回空列表。"""
    lines = text.splitlines() or [text]
    if parts <= 1 or len(lines) <= 1:
        return [text]
    size = max(1, (len(lines) + parts - 1)//parts)
    return ['\n'.join(lines[i:i + size]) for i in range(0, len(lines), size)]


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description='定位触发上游 11102 的提示词片段')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--text', help='直接给候选文本')
    source.add_argument('--file', help='从文件读候选文本（配合 --bisect 做分段二分）')
    parser.add_argument('--parts', type=int, default=8, help='--bisect 时切几段（默认 8）')
    parser.add_argument('--bisect', action='store_true', help='逐段测试，定位触发片段')
    parser.add_argument('--base-url')
    parser.add_argument('--api-key')
    parser.add_argument('--api-key-env', help='从这个环境变量取 apiKey')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--dry-run', action='store_true', help='只打印将要发送的内容，不发请求')
    args = parser.parse_args(argv)

    text = args.text if args.text is not None else Path(args.file).read_text(errors='replace')
    if not text.strip():
        print('候选文本为空', file=sys.stderr)
        return 2

    if args.dry_run:
        chunks = segments(text, args.parts) if args.bisect else [text]
        for index, chunk in enumerate(chunks):
            print('--- 片段 %d/%d（%d 字符）---' % (index + 1, len(chunks), len(chunk)))
            print(chunk[:400] + ('…' if len(chunk) > 400 else ''))
        return 0

    base_url, api_key = resolve_credentials(args.base_url, args.api_key, args.api_key_env)
    if not base_url:
        print('无法解析 baseURL：请在 --base-url 指定（profile 里应有 baseURL）', file=sys.stderr)
        return 2
    if not api_key:
        print('无法解析 apiKey：请用 --api-key / --api-key-env 指定；'
              '或确认 %s 里有 apiKey / apiKeyEnv，或 %s 里有 llm-pi-ai/<provider>.key'
              % (PROFILE, CREDENTIALS), file=sys.stderr)
        return 2

    if not args.bisect:
        result = probe(base_url, api_key, text, args.model)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result['rejected'] else 0

    chunks = segments(text, args.parts)
    hits = []
    for index, chunk in enumerate(chunks):
        result = probe(base_url, api_key, chunk, args.model)
        flag = 'REJECTED' if result['rejected'] else 'ok      '
        print('[%2d/%2d] %s status=%s  %d 字符' % (index + 1, len(chunks), flag,
                                                   result['status'], len(chunk)))
        if result['rejected']:
            hits.append(index)
    if hits:
        print('\n命中片段：%s' % ', '.join(str(i + 1) for i in hits))
        print('把命中片段里的句子逐条再跑一次 --text，缩小到句级后写进 prompt_defuse_rules。')
        return 1
    print('\n所有片段都通过 —— 触发串可能在被切开的边界上，或需要与其他内容组合；'
          '可换 --parts 再试，或用整段 --text 验证。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
