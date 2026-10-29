#!/usr/bin/env python3
"""凭据明文扫描器（R3-L6a / G4-5）。

目标：在**工作树 + evidence/ + 本地 lab VM 的 lab 目录**里找出明文凭据出现在
日志/证据/配置/错误信息里的实例。判定与分类见下方 PATTERNS 与 classify()。

硬性约束（安全）：
* **扫描器绝不把命中值写进证据**：只输出 `path:line:pattern_class:sha256[:16]`、
  长度与风险分类；不输出明文、也不输出可还原的部分片段（顶多保留前缀族别，
  例如 "wbk_" 这类已知公开前缀）。
* 只读：不修改被扫文件；不读取生产目录（VM 只扫 `/home/gateway-lab/lab`）。
* 二进制/超大文件跳过，并在 `coverage.skipped` 里如实计数。

用法：
    # 宿主：工作树 + evidence（默认）
    python -X utf8 tests/linux_lab/scan_secret_plaintext.py --out evidence/r3-l6/secret-scan.json
    # 追加扫描本地 lab VM（只读，SSH）
    python -X utf8 tests/linux_lab/scan_secret_plaintext.py --remote --out …
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / 'evidence/r3-l6/secret-scan.json'

# 与 tests/zhihui_harness/gate_checks.py --secrets 共用同一套"合成/真实"判定
# （值级标记、S1 已知指纹、证据白名单豁免）；口径变更只需改 secret_policy.py。
import sys as _sys
_sys.path.insert(0, str(REPO / 'tests'))
from zhihui_harness.secret_policy import (  # noqa: E402
    KNOWN_S1_SHA16, SYNTHETIC_MARKERS, exempt_evidence_hit)

# 合成标记 / S1 指纹 / 证据豁免规则均来自 tests/zhihui_harness/secret_policy.py
# （与 gate_checks.py --secrets 共用；不要在本地再定义一份，避免两套标准漂移）。
# 明显是文档/测试里的短值：不当作泄露（例如 'wbk_abc12345'、'wbt_read'）。
MIN_SECRET_LEN = 16

# (class, 正则, 值捕获组, 是否要求“像凭据”)
PATTERNS: list[tuple[str, re.Pattern, int]] = [
    # Authorization: Bearer <值>
    ('bearer_header', re.compile(r'[Aa]uthorization["\']?\s*[:=]\s*["\']?Bearer\s+([A-Za-z0-9._~+/=-]{16,})'), 1),
    # 内部 Bearer（Nginx 注入头 / 配置 / 日志）
    ('internal_bearer', re.compile(r'[Xx]-[Qq][Tt][Gg]-[Ii]nternal-[Aa]uth["\']?\s*[:=]\s*["\']?([A-Za-z0-9._~+/=-]{16,})'), 1),
    # Manager 推理密钥 wbk_
    ('wbk_key', re.compile(r'\b(wbk_[A-Za-z0-9._~+/=-]{8,})'), 1),
    # Manager API Token wbt_
    ('wbt_token', re.compile(r'\b(wbt_[A-Za-z0-9._~+/=-]{8,})'), 1),
    # OpenAI 风格 sk-
    ('sk_key', re.compile(r'\b(sk-[A-Za-z0-9._~+/=-]{16,})'), 1),
    # 会话 Cookie（wb_session / qtg_session）
    ('session_cookie', re.compile(r'\b((?:wb|qtg)_session=)([A-Za-z0-9%._~+/=-]{16,})'), 2),
    # 私钥 PEM 头（不含后续内容）
    ('private_key_pem', re.compile(r'(-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----)'), 1),
    # 管理口令/密钥被打印（bootstrap 输出、错误信息、配置）
    ('password_or_secret_value', re.compile(
        r'(?:password|passwd|secret|api[_-]?key|panel[_-]?password|admin[_-]?token)'
        r'["\']?\s*[:=]\s*["\']?([A-Za-z0-9._~+/=!@#%^&*-]{16,})'), 1),
]

# 高熵兜底：长串随机值（仅限出现在“疑似凭据键”附近的行）
HIGH_ENTROPY_NEAR = re.compile(
    r'(?i)(token|secret|authorization|bearer|password|api[_-]?key|cookie|credential|private)')
HIGH_ENTROPY_VALUE = re.compile(r'[A-Za-z0-9._~+/=-]{32,}')
# 只有这些"配置/日志类"后缀才做高熵兜底（压缩 JS/锁文件的噪声太大）
HIGH_ENTROPY_EXTS = {'.json', '.ndjson', '.log', '.jsonl', '.md', '.txt', '.env',
                     '.yml', '.yaml', '.ini', '.conf', '.cfg', '.xml', '.csv'}
# key:value / KEY=value 形态（值可能带引号）
_KV_LINE = re.compile(r'''["'A-Za-z0-9_.-]+["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{40,}''')
# 纯十六进制摘要（sha256=64 / sha1=40 / md5=32）
_HEX_DIGEST = re.compile(r'^[0-9a-f]{32}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$|^[0-9A-F]{32,64}$')
_DIGEST_HINT = re.compile(r'(?i)(sha256|sha1|md5|digest|fingerprint|integrity|manifest_sha)')
# npm `resolved` 链接 / 任意 URL：不是凭据
_URL_LIKE = re.compile(r'^(?://|https?:)')
# 高熵兜底真正匹配的形态：<key>[:=]<长值>（key 里必须含凭据关键词）
_HIGH_ENTROPY_KV = re.compile(r'''["']?([A-Za-z0-9_.-]{2,40})["']?\s*[:=]\s*["']?([A-Za-z0-9._~+/=-]{40,200})["']?''')
_CODE_FILE_HINT = re.compile(r'\.(go|py|js|mjs|ts|tsx|json|md|txt|yaml|yml|sql|toml|cfg|conf)$')
_URL_HINT = re.compile(r'(?i)(registry\.|\.tgz\b|\.tar\.gz\b|npmjs\.org|github\.com|'
                       r'pypi\.org|files\.pythonhosted)')
# 依赖清单文件：整份跳过高熵兜底（里面全是 URL/摘要）
_DEP_MANIFEST_NAMES = {'package-lock.json', 'web-deps-frozen.json', 'pnpm-lock.yaml',
                       'yarn.lock', 'npm-shrinkwrap.json'}

SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.next', '.pytest_cache',
             '.mypy_cache', '.ruff_cache', '.venv', 'venv', 'dist', 'build'}
SKIP_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.woff', '.woff2',
             '.ttf', '.otf', '.pdf', '.zip', '.gz', '.tar', '.qcow2', '.mp4',
             '.sqlite', '.db', '.pyc', '.so', '.dll', '.exe', '.bin', '.lnk'}
MAX_FILE_BYTES = 5 * 1024 * 1024


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = {c: text.count(c) for c in set(text)}
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


_CODE_PATH = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$')
_CODE_CALL_HINT = re.compile(r'(read_text|read_bytes|get\(|fetch|forward_api_key|'
                             r'identity_signing_key|os\.environ|getenv|self\.)')


def _looks_like_code(value: str, line: str) -> bool:
    """`password=upstreamsvc.forward_api_key(group)` 这类是**表达式**，不是凭据值。

    判定：值是点分标识符（a.b.c），或值后面紧跟 `(`，或值里出现已知取数函数名。
    这里宁可在扫描阶段放过（不报），也不要产出"看起来像泄露"的假阳性。
    """
    if _CODE_PATH.match(value):
        return True
    if _CODE_CALL_HINT.search(value):
        return True
    idx = line.find(value)
    if idx >= 0 and line[idx + len(value):idx + len(value) + 1] == '(':
        return True
    return False


def _looks_synthetic(value: str) -> bool:
    low = value.lower()
    return any(marker in low for marker in SYNTHETIC_MARKERS) or len(value) < MIN_SECRET_LEN


def _classify(context: str, value: str, path: Path, pattern_class: str,
              file_text: str = '') -> dict:
    """把命中分成 synthetic / needs-review / risk；不评价译文，只看凭据形态与上下文。"""
    synthetic_marker = _looks_synthetic(value)
    # 证据/测试目录 + 探针自身声明 production_connected=false 的上下文 → 合成
    path_text = path.as_posix()
    in_test_tree = ('/tests/' in '/' + path_text or path_text.startswith('tests/')
                    or '/evidence/' in '/' + path_text or path_text.startswith('evidence/'))
    low_ctx = context.lower()
    # 证据目录：按共享白名单 + fixture 上下文判定（与 gate_checks 同一口径）；
    # 白名单内且有上下文 → synthetic；证据但不在白名单/缺上下文 → 交后续逻辑判
    # needs-review（不阻断，但保留人工复核）。
    rel = path_text.replace('\\', '/')
    if 'evidence/' in rel:
        rel = rel[rel.index('evidence/'):]
        exempt, _reason = exempt_evidence_hit(rel, context, value, file_text)
        if exempt:
            synthetic_marker = True
        elif in_test_tree:
            in_test_tree = False  # 不在白名单的证据命中不按"测试树自动合成"处理
    declared_synthetic = any(word in low_ctx for word in
                             ('synthetic', 'fixture', 'production_connected', 'fake',
                              'placeholder', 'redacted'))
    entropy = shannon_entropy(value)
    if pattern_class in ('wbk_key', 'wbt_token', 'sk_key', 'bearer_header',
                         'internal_bearer', 'session_cookie', 'password_or_secret_value'):
        if synthetic_marker or (in_test_tree and declared_synthetic):
            risk = 'synthetic'
        elif in_test_tree:
            # 测试树里、但没有显式合成标记：值多半是夹具，但扫描器无法仅凭形态排除，
            # 记为 synthetic_test_fixture（仍然列出，便于确认；建议统一加 not-a-real 前缀）
            risk = 'synthetic'
        elif entropy >= 3.5 and len(value) >= 24:
            risk = 'risk'
        else:
            risk = 'needs-review'
    elif pattern_class == 'private_key_pem':
        # 只命中"头"；是否真含密钥需要人工确认。测试夹具通常紧挨 FIXTURE/TEST/DUMMY。
        fixture_ctx = any(word in low_ctx for word in ('fixture', 'test', 'dummy',
                                                       'example', 'sample'))
        risk = 'synthetic' if (in_test_tree or synthetic_marker or fixture_ctx) else 'risk'
    else:
        risk = 'needs-review'
    return {'risk': risk, 'entropy': round(entropy, 2),
            'synthetic_marker': synthetic_marker, 'in_test_tree': in_test_tree}


def _iter_files(root: Path, *, skipped: dict, exclude_prefixes: tuple[str, ...] = ()):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith('.git'))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() in SKIP_EXTS:
                skipped['binary_ext'] += 1
                continue
            try:
                info = path.lstat()
            except OSError:
                skipped['stat_error'] += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped['not_regular'] += 1
                continue
            if info.st_size > MAX_FILE_BYTES:
                skipped['too_large'] += 1
                continue
            rel_guess = str(path)
            if any(prefix in rel_guess.replace('\\', '/') for prefix in exclude_prefixes):
                skipped['excluded_prefix'] = skipped.get('excluded_prefix', 0) + 1
                continue
            # 跳过本扫描器自己的输出（否则会把自己的 hash 当成命中，形成自引用噪声）
            if path.name == 'secret-scan.json' and path.parent.name == 'r3-l6':
                skipped['own_output'] = skipped.get('own_output', 0) + 1
                continue
            yield path


def scan_text(path: Path, rel: str, hits: list[dict], stats: dict) -> None:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        stats['read_error'] += 1
        return
    stats['files_scanned'] += 1
    stats['bytes_scanned'] += len(text)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:  # 极长行（压缩 JS/单行 JSON）截断，避免正则回溯
            line = line[:4000]
        for pattern_class, regex, group in PATTERNS:
            for match in regex.finditer(line):
                value = match.group(group) or ''
                if not value:
                    continue
                if pattern_class == 'session_cookie':
                    value = match.group(2)
                if len(value) < 8:
                    continue
                if pattern_class in ('password_or_secret_value', 'internal_bearer',
                                     'bearer_header') and _looks_like_code(value, line):
                    continue
                info = _classify(line, value, path, pattern_class, text)
                digest = hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:16]
                hits.append({
                    'path': rel, 'line': lineno, 'class': pattern_class,
                    'sha256_16': digest, 'length': len(value),
                    'entropy': info['entropy'], 'risk': info['risk'],
                    'in_test_tree': info['in_test_tree'],
                    'synthetic_marker': info['synthetic_marker'],
                })
                stats['raw_hits'] += 1
        # 高熵兜底：只在"配置/日志类文本 + 明确的 key:value 形态"上启用。
        # 之前对所有文本都跑，会在压缩后的 JS/package-lock 里产出上千条噪声，
        # 反而淹没真命中。
        allow_high_entropy = (path.suffix.lower() in HIGH_ENTROPY_EXTS
                              and path.name not in _DEP_MANIFEST_NAMES)
        if allow_high_entropy and len(line) < 600:
            # 只认「凭据关键词 = 长随机值」的 key:value / KEY=value 形态。
            # 早先的写法是"行里出现关键词 + 任取一个长串"，结果把文件名/路径
            # （如 device_token_test.go）也算成值，产生几百条噪声。
            for kv in _HIGH_ENTROPY_KV.finditer(line):
                key, value = kv.group(1), kv.group(2)
                if not HIGH_ENTROPY_NEAR.search(key):
                    continue
                if len(value) < 40 or len(value) > 200 or _looks_synthetic(value):
                    continue
                if _looks_like_code(value, line):
                    continue
                if _HEX_DIGEST.match(value) or _DIGEST_HINT.search(line):
                    stats['hash_like_skipped'] = stats.get('hash_like_skipped', 0) + 1
                    continue
                if _URL_LIKE.match(value) or _URL_HINT.search(line):
                    stats['url_like_skipped'] = stats.get('url_like_skipped', 0) + 1
                    continue
                # 路径样（含 / 且带源码扩展名）不是凭据
                if '/' in value and _CODE_FILE_HINT.search(value):
                    stats['path_like_skipped'] = stats.get('path_like_skipped', 0) + 1
                    continue
                if shannon_entropy(value) < 4.0:
                    continue
                hits.append({
                    'path': rel, 'line': lineno, 'class': 'high_entropy_near_keyword',
                    'sha256_16': hashlib.sha256(value.encode()).hexdigest()[:16],
                    'length': len(value),
                    'entropy': round(shannon_entropy(value), 2),
                    'risk': 'needs-review' if not rel.startswith(('tests/', 'evidence/'))
                    else 'synthetic',
                    'in_test_tree': rel.startswith(('tests/', 'evidence/')),
                    'synthetic_marker': False,
                })
                stats['raw_hits'] += 1


def dedupe(hits: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for hit in hits:
        key = (hit['path'], hit['line'], hit['class'], hit['sha256_16'])
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def scan_host(roots: list[Path], *, skipped: dict, stats: dict,
              exclude_prefixes: tuple[str, ...] = ()) -> tuple[list[dict], dict]:
    hits: list[dict] = []
    coverage = {}
    for root in roots:
        if not root.exists():
            coverage[str(root)] = 'missing'
            continue
        before = stats['files_scanned']
        for path in _iter_files(root, skipped=skipped,
                                exclude_prefixes=exclude_prefixes):
            try:
                rel = path.relative_to(REPO).as_posix()
            except ValueError:
                rel = path.as_posix()  # --roots 指向仓库外（CI 临时目录）时按绝对路径记录
            scan_text(path, rel, hits, stats)
        coverage[str(root.relative_to(REPO) if root.is_relative_to(REPO) else root)] = \
            f'scanned ({stats["files_scanned"] - before} files)'
    return hits, coverage


REMOTE = 'gateway-lab@127.0.0.1'
REMOTE_LAB = '/home/gateway-lab/lab'
LAB_KEY = Path(r'D:\AI\codex\codex1\zhihui-linux-lab\lab_ed25519')
LAB_KNOWN = Path(r'D:\AI\codex\codex1\zhihui-linux-lab\known_hosts')


def _ssh(args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = ['ssh.exe' if os.name == 'nt' else 'ssh', '-F', 'NUL' if os.name == 'nt' else '/dev/null',
           '-p', '22224', '-i', str(LAB_KEY), '-o', 'BatchMode=yes',
           '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={LAB_KNOWN}',
           '-o', 'ConnectTimeout=10', REMOTE, args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout)


def scan_remote(*, stats: dict) -> tuple[list[dict], dict]:
    """只读扫描 VM 的 lab 目录：**按目录分段 grep**，避免单次输出上限造成漏扫。

    为什么分段：lab 目录 2.8GB / 上千个 bundle 副本，一次 `grep | head` 会在
    前 20000 行截断，导致排在后面的目录（例如 `snapshots/`，正是 S1 旧值所在）
    整段漏掉——那会让"扫描通过"变成假阴性。这里：
      1. `snapshots/` 单独扫（上限 40000，S1 旧值就在这）；
      2. 其余目录一起扫（排除 snapshots，上限 20000）；
      3. 根目录下的散文件单独扫；
      4. 每段都记录 candidate_lines/truncated，汇总到 coverage。
    """
    if not LAB_KEY.exists():
        return [], {'remote': 'skipped (lab key missing)'}
    probe = _ssh(f'test -d {REMOTE_LAB} && echo ok', timeout=30)
    if probe.returncode != 0 or 'ok' not in probe.stdout:
        return [], {'remote': f'unreachable (exit={probe.returncode})'}
    pattern = (r'authorization|Bearer|wbk_|wbt_|sk-[A-Za-z0-9]|X-QTG-Internal-Auth|'
               r'private_key|PRIVATE KEY|password|secret|session')
    grep_base = ('grep -rInE --binary-files=without-match -m 5 '
                 '--exclude-dir=_next --exclude-dir=node_modules --exclude-dir=.git '
                 '--exclude=*.map --exclude=*.woff2 --exclude=*.woff '
                 f'{json.dumps(pattern)} ')
    segments: list[tuple[str, str]] = []
    snapshots = f'{REMOTE_LAB}/snapshots'
    # snapshots 下每个快照都是一份 workspace 副本，命中量大；按批 grep，
    # 每批单独设上限，避免"单次 head 截断"漏掉排在后面的快照（S1 旧值就在其中）。
    snap_dirs = []
    if _ssh(f'test -d {snapshots} && echo yes', timeout=30).returncode == 0:
        listing = _ssh(f'ls -d {snapshots}/*/ 2>/dev/null', timeout=60).stdout or ''
        snap_dirs = [line.strip() for line in listing.splitlines() if line.strip().endswith('/')]
    batch_size = 8
    for index in range(0, len(snap_dirs), batch_size):
        chunk = snap_dirs[index:index + batch_size]
        segments.append((f'snapshots_batch_{index // batch_size}',
                         ' '.join(chunk) + ' 2>/dev/null | head -n 30000'))
    if not snap_dirs:
        segments.append(('snapshots', f'{snapshots} 2>/dev/null | head -n 30000'))
    segments.append(('other_dirs',
                     f'{REMOTE_LAB} --exclude-dir=snapshots 2>/dev/null | head -n 20000'))
    segments.append(('root_files',
                     f'$(find {REMOTE_LAB} -maxdepth 1 -type f 2>/dev/null) '
                     '2>/dev/null | head -n 5000'))

    hits: list[dict] = []
    coverage: dict = {'remote_root': REMOTE_LAB, 'segments': {}}
    remote_files: set[str] = set()
    for name, tail in segments:
        proc = _ssh(grep_base + tail, timeout=300)
        seg = {'exit': proc.returncode, 'candidate_lines': 0, 'truncated': False}
        for line in (proc.stdout or '').splitlines():
            parts = line.split(':', 2)
            if len(parts) != 3 or not parts[0].startswith('/'):
                continue
            path, lineno, text = parts
            seg['candidate_lines'] += 1
            remote_files.add(path)
            for pattern_class, regex, group in PATTERNS:
                for match in regex.finditer(text):
                    value = match.group(group) or ''
                    if pattern_class == 'session_cookie':
                        value = match.group(2)
                    if len(value) < 8:
                        continue
                    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
                    risk = 'synthetic'
                    if digest == KNOWN_S1_SHA16:
                        risk = 'known-s1'
                    elif not (_looks_synthetic(value) or '/lab/' in path):
                        risk = 'needs-review'
                    hits.append({
                        'path': path, 'line': int(lineno) if lineno.isdigit() else 0,
                        'class': pattern_class, 'sha256_16': digest,
                        'length': len(value),
                        'entropy': round(shannon_entropy(value), 2),
                        'risk': risk,
                        'in_test_tree': True,
                        'synthetic_marker': _looks_synthetic(value),
                        'remote': True,
                    })
                    stats['raw_hits'] += 1
        limit = 30000 if name.startswith('snapshots') else (5000 if name == 'root_files' else 20000)
        seg['truncated'] = seg['candidate_lines'] >= limit
        coverage['segments'][name] = seg
    coverage['files_with_candidates'] = len(remote_files)
    coverage['truncated'] = any(seg['truncated'] for seg in coverage['segments'].values())
    return hits, coverage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='凭据明文扫描（R3-L6a）')
    ap.add_argument('--out', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--remote', action='store_true', help='追加只读扫描本地 lab VM 的 lab 目录')
    ap.add_argument('--roots', default='', help='额外宿主根目录（逗号分隔，默认整体工作树）')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--exclude-evidence', action='store_true',
                    help='跳过 evidence/（CI 门禁常用：证据里允许合成夹具值）')
    ap.add_argument('--fail-on-risk', action='store_true',
                    help='存在 risk/known-s1（疑似真实凭据）时以非零退出（CI 守卫用）；'
                         'synthetic/needs-review 不阻断')
    args = ap.parse_args(argv)

    started = time.time()
    skipped = {'binary_ext': 0, 'too_large': 0, 'not_regular': 0, 'stat_error': 0}
    stats = {'files_scanned': 0, 'bytes_scanned': 0, 'raw_hits': 0, 'read_error': 0}
    exclude_prefixes = ('/evidence/',) if args.exclude_evidence else ()
    hits, coverage = scan_host([REPO], skipped=skipped, stats=stats,
                               exclude_prefixes=exclude_prefixes)
    if args.roots:
        extra = [Path(p) for p in args.roots.split(',') if p.strip()]
        more, extra_cov = scan_host(extra, skipped=skipped, stats=stats,
                                    exclude_prefixes=exclude_prefixes)
        hits.extend(more)
        coverage.update(extra_cov)
    remote_cov = {}
    if args.remote:
        more, remote_cov = scan_remote(stats=stats)
        hits.extend(more)
        coverage['remote'] = remote_cov

    hits = dedupe(hits)
    by_risk: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for hit in hits:
        by_risk[hit['risk']] = by_risk.get(hit['risk'], 0) + 1
        by_class[hit['class']] = by_class.get(hit['class'], 0) + 1
    report = {
        'schema': 'zhihui-r3-l6-secret-scan/v1',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'scanner_note': '只记录 path:line:class:sha256[:16]/length/entropy/risk；'
                        '不记录明文，也不记录可还原的片段',
        'scope': {'roots': [str(r) for r in ([REPO] + ([Path(p) for p in args.roots.split(',')]
                                                          if args.roots else []))],
                  'remote': bool(args.remote)},
        'coverage': coverage,
        'stats': {**stats, 'unique_hits': len(hits),
                  'skipped': skipped, 'elapsed_s': round(time.time() - started, 2)},
        'summary': {'by_risk': by_risk, 'by_class': by_class},
        'hits': sorted(hits, key=lambda h: ({'risk': 0, 'needs-review': 1,
                                             'synthetic': 2}.get(h['risk'], 3),
                                            h['class'], h['path'], h['line'])),
        'class_meaning': {
            'bearer_header': 'Authorization: Bearer 头里的值',
            'internal_bearer': 'X-QTG-Internal-Auth（Nginx 注入的内部凭据）',
            'wbk_key': 'Manager 推理密钥', 'wbt_token': 'Manager API token',
            'sk_key': 'OpenAI 风格 sk- 密钥', 'session_cookie': 'wb_session/qtg_session 会话值',
            'private_key_pem': '私钥 PEM 头（是否真含密钥需人工确认）',
            'password_or_secret_value': 'password/secret/api_key 被写成明文值',
            'high_entropy_near_keyword': '凭据关键词附近的长高熵串（兜底）',
        },
        'remote_classification_note': (
            'VM 的 /home/gateway-lab/lab 是一次性 lab 环境，远端命中默认归 synthetic；'
            '唯一例外是已知 S1 旧值（sha256[:16]=5e103bca9f6d53e8），标记为 known-s1，'
            '表示"旧值仍存在于历史快照/旧包中"，需按 S1 处置（重新打包/清快照由 Lead 决策）。'),
        'risk_meaning': {
            'risk': '需立即人工确认：非测试树、无明显合成标记、形态像真实凭据',
            'needs-review': '可能是测试/演练/文档值，但无法仅凭形态排除，建议人工过一眼',
            'synthetic': '有合成标记或位于测试/证据树且上下文声明 production_connected=false',
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    brief = {'out': str(args.out), 'unique_hits': len(hits),
             'by_risk': by_risk, 'by_class': by_class}
    blocking = by_risk.get('risk', 0) + by_risk.get('known-s1', 0)
    brief['fail_on_risk'] = bool(args.fail_on_risk)
    brief['blocking_hits'] = blocking
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    if args.fail_on_risk and blocking:
        print(f'[secret-scan] 发现 {blocking} 条疑似真实凭据（risk/known-s1），按 --fail-on-risk 失败',
              file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
