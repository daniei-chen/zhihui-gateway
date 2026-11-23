#!/usr/bin/env python3
"""逐项复现启动门禁，在**切换发布指针之前**回答「这个版本能不能起来」。

## 为什么需要它

`writer_entry.py` 把所有失败压成同一句话：

    WRITER-LAUNCH-BLOCKED: host release/state/contract rejected

于是「清单里 `release_id` 没同步」「发布树里混进 `__pycache__`」「前端 manifest
签名不对」在日志里长得**一模一样**，只能靠猜。2026-11-16 一次发布因此让网关
连续 15 分钟不可用 —— 每次重启都立刻被拒，而日志没有提供任何可区分的线索。

这个脚本把同一套判据逐项跑一遍，每项单独报 OK/FAIL。它只读，不启动服务、
不改任何文件，因此可以在任何时候对候选目录跑。

## 用法

    deploy/preflight-gate.py /opt/workbuddy-manager/releases/v1.3.3

退出码 0 表示各项全过；非 0 表示至少一项失败，失败项会打印异常与栈。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import traceback
from pathlib import Path

#: 信任公钥。允许用环境变量覆盖：发布镜像里没有这个路径（它是宿主文件），
#: 需要挂到别处再指过来。硬编码过一次，结果门禁在容器里根本跑不起来 ——
#: 而容器正是切换指针前唯一能跑 python 的地方。
TRUST = Path(os.environ.get('WB_RELEASE_TRUST',
                           '/opt/workbuddy-manager/release-trust-ed25519.pem'))


def _step(label, fn):
    try:
        print(f'[ OK ] {label}: {fn()}')
        return True
    except Exception as exc:                      # noqa: BLE001 - 诊断工具要报告一切
        print(f'[FAIL] {label}: {type(exc).__name__}: {exc}')
        traceback.print_exc(limit=2)
        return False


def _openssl(manifest: Path, signature: Path) -> str:
    """校验分离签名：**cryptography 优先，宿主 openssl 回退**。

    为什么不能只调 openssl：发布镜像（alpine）里没有它，而门禁必须在
    **切换指针之前**就能跑 —— 那正是容器里唯一能跑 python 的地方。
    两套实现都是「对原始字节做 Ed25519 验签」，结论等价；签名本身是确定性
    的，不存在「一边过一边不过」。

    装了 cryptography 却验签失败时**直接报错**，不回退 —— 否则会变成
    「库说不对，那就当它对」，把失败吞掉。
    """
    data = manifest.read_bytes()
    sig = signature.read_bytes()
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        pass
    else:
        try:
            load_pem_public_key(TRUST.read_bytes()).verify(sig, data)
        except InvalidSignature:
            raise AssertionError(f'signature rejected: {signature.name}') from None
        return 'Signature Verified (cryptography)'
    proc = subprocess.run(
        ['/usr/bin/openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(TRUST),
         '-rawin', '-in', str(manifest), '-sigfile', str(signature)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError((proc.stdout or proc.stderr).strip() or 'signature rejected')
    return 'Signature Verified'


def main(argv) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    root = Path(argv[0]).resolve(strict=True)
    print(f'== 目标发布: {root} (name={root.name}) ==')

    manifest_path = root / 'deploy/managed-release.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for key in ('version', 'release_id', 'signature_algorithm', 'signature_required'):
        print(f'   manifest.{key:20s} = {manifest.get(key)!r}')
    print(f'   dir name             = {root.name!r}')

    ok = True

    def same(field):
        def check():
            value = manifest.get(field)
            if value != root.name:
                raise AssertionError(f'{value!r} != {root.name!r}')
            return 'match'
        return check

    ok &= _step('version == dir', same('version'))
    ok &= _step('release_id == dir', same('release_id'))

    def check_files():
        bad = []
        for relative, item in manifest['files'].items():
            path = root / relative
            if not path.is_file():
                bad.append(f'missing:{relative}')
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['candidate_sha256']:
                bad.append(f'hash:{relative}')
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            if mode != item['mode']:
                bad.append(f'mode:{relative}({mode}!={item["mode"]})')
        if bad:
            raise AssertionError(f'{len(bad)} 项不符: {bad[:8]}')
        return f'{len(manifest["files"])} 文件全部一致'

    ok &= _step('逐文件哈希与 mode', check_files)
    ok &= _step('清单 Ed25519 签名',
                lambda: _openssl(manifest_path, root / 'deploy/managed-release.sig'))

    def check_closure():
        """双向闭包：发布树里的文件集合必须与清单完全一致。

        两个方向都会出事：
          · 多出文件（例如跑测试留下的 `__pycache__`）→ 门禁报
            `release contains executable bytecode cache`；
          · 少文件（清单引用了不存在的路径）→ 门禁报 `source closure differs`。
        """
        actual = set()
        excluded = {'deploy/managed-release.json', 'deploy/managed-release.sig'}
        for name in ('server', 'deploy'):
            base = root / name
            if not base.is_dir():
                raise AssertionError(f'缺目录 {name}')
            for path in base.rglob('*'):
                if path.is_dir():
                    if path.name == '__pycache__':
                        raise AssertionError('发布树里有 __pycache__（门禁会拒绝）')
                    continue
                if path.suffix in ('.pyc', '.pyo'):
                    raise AssertionError(f'发布树里有字节码 {path.relative_to(root)}')
                relative = path.relative_to(root).as_posix()
                if relative not in excluded:
                    actual.add(relative)
        expected = {p for p in manifest['files'] if p.startswith(('server/', 'deploy/'))}
        extra, missing = actual - expected, expected - actual
        if extra or missing:
            raise AssertionError(
                f'多出 {len(extra)} 项 {sorted(extra)[:5]} / 缺 {len(missing)} 项 {sorted(missing)[:5]}')
        return f'{len(actual)} 项双向一致'

    ok &= _step('server+deploy 双向闭包', check_closure)

    def check_caps():
        rel = 'server/writer-capabilities.json'
        entry = manifest['files'].get(rel)
        if entry is None:
            raise AssertionError('writer-capabilities.json 不在清单里')
        path = root / rel
        raw = path.read_bytes()
        value = json.loads(raw)
        problems = []
        if hashlib.sha256(raw).hexdigest() != entry['candidate_sha256']:
            problems.append('哈希与清单不符')
        if stat.S_IMODE(path.lstat().st_mode) != entry['mode']:
            problems.append('mode 与清单不符')
        if value.get('release_id') != root.name:
            problems.append(f'release_id={value.get("release_id")!r}')
        if value.get('version') != 1:
            problems.append(f'version={value.get("version")!r}')
        if value.get('schema') != 'zhihui.writer-release':
            problems.append(f'schema={value.get("schema")!r}')
        if problems:
            raise AssertionError('; '.join(problems))
        return '绑定正确'

    ok &= _step('writer-capabilities 绑定', check_caps)

    def check_web():
        base = root / 'web/out'
        mf, sig = base / 'manifest.sha256', base / 'manifest.sig'
        for path in (mf, sig):
            if not path.is_file():
                raise AssertionError(f'缺 {path}')
        _openssl(mf, sig)
        lines = [ln for ln in mf.read_text(encoding='utf-8').splitlines() if ln.strip()]
        missing, bad = [], []
        for line in lines:
            # 格式是 `sha256  字节数  相对路径`（三段）。按两段切会把 size 当成
            # 路径的一部分，于是每个文件都报"缺失"。
            parts = line.split('  ')
            if len(parts) != 3:
                continue
            digest, size_text, name = parts[0].strip(), parts[1].strip(), parts[2].strip()
            path = base / name
            if not path.is_file():
                missing.append(name)
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                bad.append(name)
            elif size_text.isdigit() and len(raw) != int(size_text):
                bad.append(f'{name}(size)')
        if missing or bad:
            raise AssertionError(f'缺 {len(missing)} / 不符 {len(bad)}: {(missing + bad)[:6]}')
        return f'{len(lines)} 个前端文件一致'

    ok &= _step('前端闭包签名与逐文件', check_web)

    def check_pointer():
        """发布指针的健康检查。

        三种情况要分开，因为它们的含义完全不同：

        · **写成绝对路径** → 失败。本门禁常在容器里跑（`--root /rel`），绝对名
          会把容器路径烙进宿主文件系统；发布机重启后服务解析不到目标。真实事故。
        · **解析失败** → 失败。目标目录不存在，指针已经坏了。
        · **指向另一个版本** → 只有提示。切换前跑本检查时指针本来就指着旧版本，
          这属于正常流程，不能因此拦住发布。
        """
        pointer = root.parent / 'current'
        if not pointer.is_symlink():
            return '跳过（current 不是符号链接）'
        raw = os.readlink(pointer)
        if os.path.isabs(raw):
            raise AssertionError(
                f'current -> {raw} 写成了绝对路径；应写相对名，否则容器与宿主解析不一致')
        try:
            target = pointer.resolve(strict=True)
        except FileNotFoundError:
            raise AssertionError(f'current -> {raw} 解析失败（目标不存在）') from None
        if target != root:
            return f'current -> {raw}（尚未切换到本目录，正常）'
        return f'current -> {raw}'

    ok &= _step('发布指针', check_pointer)

    print()
    print('== 全部通过，可以切换发布指针 ==' if ok else '== 有失败项，不要切换发布指针 ==')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
