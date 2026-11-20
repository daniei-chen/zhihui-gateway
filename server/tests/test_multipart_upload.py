"""守住 multipart 解析的两个真实坑（2026-11-20 豆包上传）。

事故 1：`_split_multipart` 本身没问题，但调用方先对整条 content-type 做了
        `.lower()` 再取 boundary —— boundary 区分大小写，一变小写就永远
        匹配不上 body 里的分隔符，每个上传都返回「没有账号条目」。
事故 2：前端 `accountsApi.upload` 发的是 multipart（字段名 files），
        后端第一版只按裸 JSON 解析，真机必然失败。

这里直接测 `_split_multipart`，并测「从原始 content-type 取 boundary」的规则，
不依赖起服务。
"""
from __future__ import annotations

import unittest

from server.qtgcore.routers.compat import _split_multipart

# 大小写混合的 boundary —— 与真实客户端生成的一致，是事故的触发条件。
BOUNDARY = '----zhihuiProbeA2161f249E5f4e4fb0F7f63d989c1c6B'


def _build(parts: list[tuple[str, bytes]], boundary: str = BOUNDARY) -> bytes:
    out = []
    for name, payload in parts:
        out.append(('--' + boundary).encode())
        out.append(
            ('Content-Disposition: form-data; name="files"; filename="%s"\r\n'
             'Content-Type: application/json\r\n\r\n' % name).encode())
        out.append(payload)
        out.append(b'\r\n')
    out.append(('--' + boundary + '--\r\n').encode())
    return b''.join(out)


class SplitMultipartTests(unittest.TestCase):
    def test_single_file(self) -> None:
        body = _build([('acc1.json', b'{"uid":"u1"}')])
        parts = _split_multipart(body, BOUNDARY)
        self.assertEqual(1, len(parts))
        self.assertEqual('acc1.json', parts[0][0])
        self.assertEqual(b'{"uid":"u1"}', parts[0][1])

    def test_two_files(self) -> None:
        body = _build([('a.json', b'{"uid":"a"}'), ('b.json', b'{"uid":"b"}')])
        parts = _split_multipart(body, BOUNDARY)
        self.assertEqual(['a.json', 'b.json'], [p[0] for p in parts])
        self.assertEqual([b'{"uid":"a"}', b'{"uid":"b"}'], [p[1] for p in parts])

    def test_quoted_boundary_in_header_is_unquoted_by_caller(self) -> None:
        """调用方负责剥引号；剥完必须能正常拆。"""
        body = _build([('a.json', b'{"uid":"a"}')])
        header = 'multipart/form-data; boundary="%s"' % BOUNDARY
        # 复刻端点里的取法：只对键名做 lower，值保持原样
        boundary = ''
        for part in header.split(';'):
            part = part.strip()
            if part.lower().startswith('boundary='):
                boundary = part[len('boundary='):].strip().strip('"')
                break
        self.assertEqual(BOUNDARY, boundary)
        self.assertEqual(1, len(_split_multipart(body, boundary)))

    def test_lowercased_boundary_does_not_match(self) -> None:
        """反证：把 boundary 变小写就拆不出来 —— 这就是事故 1 的形状。

        这条断言的意义是「解释为什么不能 lower」，不是鼓励这么用。
        """
        body = _build([('a.json', b'{"uid":"a"}')])
        self.assertEqual(0, len(_split_multipart(body, BOUNDARY.lower())))

    def test_header_key_case_insensitive_but_value_kept(self) -> None:
        """`Boundary=` 这种写法也要认（HTTP 头名不区分大小写），但值原样。"""
        header = 'multipart/form-data; Boundary=%s' % BOUNDARY
        boundary = ''
        for part in header.split(';'):
            part = part.strip()
            if part.lower().startswith('boundary='):
                boundary = part[len('boundary='):].strip().strip('"')
                break
        self.assertEqual(BOUNDARY, boundary)

    def test_trailing_crlf_stripped(self) -> None:
        body = _build([('a.json', b'{"uid":"a"}')])
        parts = _split_multipart(body, BOUNDARY)
        self.assertFalse(parts[0][1].endswith(b'\r\n'))

    def test_empty_body_yields_nothing(self) -> None:
        self.assertEqual([], _split_multipart(b'', BOUNDARY))


if __name__ == '__main__':
    unittest.main()
