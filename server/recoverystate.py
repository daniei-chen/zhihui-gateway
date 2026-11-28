"""恢复包的数据根不变量（G-09 / D-12）。

**为什么单独一个模块**：`backup_application.py` 在 `import fcntl` 时就只能在 POSIX
上加载，而「根下每个文件要么进包、要么带理由排除」这条判据本身与平台无关。
放在这里才能在任何机器上直接对它做红→绿与变异验证，而不是靠「反正 CI 在 Linux 跑」。

不变量：

    STATE_ROOTS 下每个普通文件 ∈ 进包 ∪ 带理由排除

上一版（2026-11-28 之前）是 5 个硬编码文件名 + 三个**只写目录条目**的数据根，
于是 `tar -tzf` 里能看到 `manager/data` 却一个文件都没有：清单看起来覆盖了，
内容是空的。新增的状态文件默认漏掉，而备份照样报 `ok`。
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import stat
import tarfile

#: 数据根：`家族` + 相对该家族的目录。家族只有两个 —— 恢复包里的
#: `manager/…` 与 `gateway/…` 前缀就是这么来的。
STATE_ROOTS = (('manager', 'data'), ('gateway', 'data'), ('gateway', 'auths'))

#: 显式排除：`家族` + 根内相对路径 glob + **理由**。
#: 没有理由的排除等于没排除（测试会断言理由非空）。
STATE_EXCLUSIONS = (
    ('manager', 'manager.db', '用 sqlite backup API 打一致性快照 → manager/data/manager.db'),
    ('manager', 'manager.db-wal', '与 manager.db 同源，一致性快照里由 sqlite 处理'),
    ('manager', 'manager.db-shm', '与 manager.db 同源，一致性快照里由 sqlite 处理'),
    ('manager', '*.tmp', '原子写的临时文件，随时会被 os.replace 吃掉'),
    ('manager', '*.lock', '进程锁，恢复后无意义'),
    ('manager', 'update-package.tar.gz', '更新过程中的中间包，可重新下载'),
    ('manager', 'update.log', '更新日志，恢复后可重建'),
    ('manager', 'version-check.json', '版本检查缓存，可重建'),
    ('manager', 'archives/**', '导出产物，不是运行时状态（可由恢复包本身重新生成）'),
)

#: 只有本次真的打了 paired snapshot 才排除这三项 —— 否则它们必须按默认路径进包，
#: 不然「没有门户库」的部署会把账本 spool 静默漏在包外。
PAIRED_STATE = (
    ('manager', 'user-portal.sqlite3', 'paired snapshot 里的一致性副本'),
    ('manager', 'user-portal-auths/**', 'paired snapshot 里的一致性副本'),
    ('manager', 'ledger-spool/**', 'paired snapshot 里的一致性副本'),
)

#: 还没纳入恢复包的数据根。只要它还在，备份状态就必须自报「覆盖不完整」：
#: 一个只覆盖两处的备份看起来像覆盖了全部，比没有断言更危险。
#: PR2（qtg 引擎目录）落地后清空。
UNCOVERED_STATE_ROOTS = (
    ('qtg-engine-var', 'qtgcore 引擎数据根（qtg.db / state.json / audit.jsonl / 兼容态 json）'),
)


def exclusion_reason(relative, family, patterns):
    """`relative` 命中哪条排除；没命中返回 `None`。"""
    for owner, pattern, reason in patterns:
        if owner != family:
            continue
        if pattern.endswith('/**'):
            prefix = pattern[:-2]
            if relative == pattern[:-3] or relative.startswith(prefix):
                return reason
        elif fnmatch.fnmatchcase(relative, pattern):
            return reason
    return None


def collect_state_entries(manager_root, gateway_root, paired):
    """把每个数据根解析成「进包 / 带理由排除 / 无法归类」。

    返回 `(entries, excluded, unexpected)`：
    `entries` 是 `(包内路径, 源文件)`；`excluded` / `unexpected` 是
    `{'path':…, 'reason':…}` 的列表。`unexpected` 非空时调用方**必须**让备份失败：
    那意味着根下出现了无法归类的条目（符号链接，或数据根本身是链接），
    静默跳过就是这次修复要消灭的那类谎。
    """
    roots = {'manager': Path(manager_root), 'gateway': Path(gateway_root)}
    patterns = tuple(STATE_EXCLUSIONS) + (tuple(PAIRED_STATE) if paired else ())
    entries, excluded, unexpected = [], [], []
    for family, directory in STATE_ROOTS:
        root = roots[family] / directory
        if root.is_symlink():
            unexpected.append({'path': family + '/' + directory, 'reason': '数据根是符号链接'})
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root).as_posix()
            name = family + '/' + directory + '/' + relative
            if path.is_symlink():
                unexpected.append({'path': name, 'reason': '符号链接'})
                continue
            if path.is_dir():
                continue
            # FIFO / socket / 设备节点**不能当普通文件收**：`open('rb')` 在 FIFO 上是
            # **阻塞**而不是异常 —— 当晚备份会一直挂住，没有异常、没有退出码，
            # 第二天只能靠人发现「备份没跑完」。挂住比失败更难发现，而备份是保护
            # 数据的那条线。所以这里按 `lstat` 判类型（不跟随链接，链接上面已经挡掉）。
            if not stat.S_ISREG(path.lstat().st_mode):
                unexpected.append({'path': name, 'reason': '不是普通文件（FIFO/socket/设备节点）'})
                continue
            reason = exclusion_reason(relative, family, patterns)
            if reason:
                excluded.append({'path': name, 'reason': reason})
            else:
                entries.append((name, path))
    return entries, excluded, unexpected


def uncovered_roots():
    """当前已知但未覆盖的数据根，给报告/状态用。"""
    return [{'root': name, 'reason': reason} for name, reason in UNCOVERED_STATE_ROOTS]


def add_state_file(archive, name, path):
    """流式写一个状态文件，返回 `(uid, gid)`。

    不能用整份读内存的写法：`admission-cohort.db` 这类文件可以很大，
    2026-11-25 那次 OOM 就是整份读进内存导致的。

    枚举那一刻判过类型，但枚举与打开之间还有窗口：那一瞬被换成 symlink 或 FIFO，
    `open` 要么跟随到别的文件、要么**在 FIFO 上阻塞**。`O_NOFOLLOW | O_NONBLOCK`
    让两种情形都变成立即报错而不是挂住，随后用 `fstat` 复核类型 —— 元数据也取自
    同一个 fd，不再二次 `stat`。
    """
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise RuntimeError('state file is not a regular file: ' + name)
    info = tarfile.TarInfo(name)
    info.size = metadata.st_size
    info.mode = 0o600
    info.mtime = int(metadata.st_mtime)
    info.uid, info.gid = metadata.st_uid, metadata.st_gid
    with os.fdopen(descriptor, 'rb') as stream:
        archive.addfile(info, stream)
    return metadata.st_uid, metadata.st_gid


def empty_archive_directories(archive):
    """目录条目下没有任何文件的那些条目名。

    `add_state_directory()` 只写一行 DIRTYPE，于是 `tar -tzf` 里能看到
    `manager/data` 却一个文件都没有 ——「清单里有这个目录」不等于「内容备份了」。
    """
    members = archive.getmembers()
    files = [member.name for member in members if member.isfile()]
    return sorted({member.name for member in members if member.isdir()
                   and not any(name.startswith(member.name + '/') for name in files)})
