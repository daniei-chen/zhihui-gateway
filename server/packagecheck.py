"""Bounded, no-extraction inspection. Structure never proves publisher trust."""
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
import zlib

MAX_COMPRESSED = 63 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_MEMBERS = 20000
BLOCK = 64 * 1024


def inspect_package(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_COMPRESSED:
        raise ValueError('升级包文件或压缩大小无效')
    try:
        with tempfile.TemporaryFile() as expanded, path.open('rb') as source:
            decoder = zlib.decompressobj(31)
            total = 0
            for chunk in iter(lambda: source.read(BLOCK), b''):
                if decoder.eof:
                    raise ValueError('升级包包含多段压缩或尾随内容')
                pending = chunk
                while pending:
                    raw = decoder.decompress(pending, min(BLOCK, MAX_EXPANDED - total + 1))
                    total += len(raw)
                    if total > MAX_EXPANDED:
                        raise ValueError('升级包展开体积超过上限')
                    expanded.write(raw)
                    pending = decoder.unconsumed_tail
                    if decoder.unused_data:
                        raise ValueError('升级包包含多段压缩或尾随内容')
            if not decoder.eof:
                raise ValueError('升级包压缩数据未完整结束')
            expanded.seek(0)
            names, version, end, count = set(), None, 0, 0
            with tarfile.open(fileobj=expanded, mode='r:') as archive:
                for member in archive:
                    count += 1
                    name = member.name.rstrip('/') if member.isdir() else member.name
                    p = PurePosixPath(name)
                    if (count > MAX_MEMBERS or not name or len(name) > 1024
                            or p.is_absolute() or p.as_posix() != name or '..' in p.parts
                            or '\\' in name or ':' in name or any(ord(c) < 32 for c in name)
                            or name.casefold() in names or member.issparse() or member.pax_headers
                            or not (member.isfile() or member.isdir())
                            or member.size > MAX_FILE or member.isdir() and member.size):
                        raise ValueError('升级包成员重复、越界或类型/大小不受支持')
                    if any(part.rstrip(' .') != part or part in ('.', '') for part in p.parts):
                        raise ValueError('升级包路径不规范')
                    names.add(name.casefold())
                    end = member.offset_data + ((member.size + 511) // 512) * 512
                    if p.name == '.version':
                        if version is not None or len(p.parts) > 2 or not member.isfile() or member.size > 128:
                            raise ValueError('升级包版本标记重复或无效')
                        version = archive.extractfile(member).read().decode('ascii').strip()
                        if not re.fullmatch(r'v?\d+\.\d+\.\d+(?:[+-][a-zA-Z0-9._-]+)?', version):
                            raise ValueError('升级包版本标记无效')
            # tarfile stops at the first zero header; hidden second archives
            # must not escape inspection. Require both terminating records.
            if total < end + 1024:
                raise ValueError('升级包归档未完整结束')
            expanded.seek(end)
            if any(any(chunk) for chunk in iter(lambda: expanded.read(BLOCK), b'')):
                raise ValueError('升级包归档后含未检查内容')
            if version is None:
                raise ValueError('升级包缺少 .version')
            return {'version': version, 'members': count, 'expanded_bytes': total,
                    'publisher_verified': False}
    except (OSError, EOFError, UnicodeError, zlib.error, tarfile.TarError) as exc:
        raise ValueError('升级包读不了：损坏或格式不完整') from exc
