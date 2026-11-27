"""日边界口径统一（D-42）：网关侧「今天 / 近 N 天」必须与 `db.day_of` 同口径。

背景
----
`server/db.py` 把「哪一天」钉死在固定 UTC+8（`day_of` / `day_start_ts` / `day_sql`，
见 `server/db.py:100` 起的说明），`usage_daily.day` 也是按这个口径写进去的。
但 `server/routers/gateway.py` 的 `_usage_since` 与 `usage_v1` 的「今天」原先用
`time.localtime` / `time.strftime('%Y-%m-%d')` 跟着**进程时区**走。

CI 在 UTC 上跑，16:00Z–24:00Z（北京 00:00–08:00）这段窗口里 UTC 日期与 CST 差一天，
于是 `last_7d` / `today` 的断言整体错位一格（PR #62 在 17:31Z 红了 7 条，同一份
代码在 14:43Z 全绿 —— 差别只在跑的时刻）。

本文件锁住三件事
----------------
1. `_usage_since(N)` 与 `db.day_of(now - (N-1)*86400)` 恒等，`_usage_today()` 与
   `db.day_of()` 恒等；
2. `/v1/usage` 的 `today` 段取的就是 `db.day_of()` 那一行；
3. 以上结果在「进程时区 = UTC」与「= Asia/Shanghai」两种情形下逐字相同。

时间戳固定、时钟用 monkeypatch 钉住，结论与「现在几点」无关。第 3 条用子进程
强制 `TZ`（Windows 的 C 运行时不认 IANA 名，退化成 `UTC-8` 也是 +0800，见
`test_usage_timezone_forcing.py`）；若一个环境连两个时区都强制不出来，该用例显式
skip 并写明原因 —— 其余用例在任何时区下仍然有效。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402
from server.routers import gateway  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

#: 2026-11-28 00:00:00 +08:00 == 2026-11-27 16:00:00Z —— 正好踩在 UTC 日期与
#: 北京日期差一天的那个窗口（16:00Z–24:00Z）里。写死数值，不依赖进程时区。
_FIXED_TS = 1795800000
_DAY = '2026-11-28'
_DAY_6 = '2026-11-22'
_DAY_29 = '2026-10-30'


class UsageDayHelpersTest(unittest.TestCase):
    """`_usage_since` / `_usage_today` 只能按 `db.day_of` 换算。"""

    def test_usage_since_matches_day_of(self) -> None:
        with mock.patch('time.time', return_value=_FIXED_TS):
            for days in (1, 7, 30):
                self.assertEqual(
                    gateway._usage_since(days),
                    db.day_of(_FIXED_TS - (days - 1) * 86400),
                    f'_usage_since({days}) 与 db.day_of 口径不一致',
                )

    def test_usage_since_is_cst_not_process_zone(self) -> None:
        """固定时刻落在 UTC 与 CST 差一天的位置，结果必须是 CST 的日期。"""
        with mock.patch('time.time', return_value=_FIXED_TS):
            self.assertEqual(gateway._usage_since(1), _DAY)
            self.assertEqual(gateway._usage_since(7), _DAY_6)
            self.assertEqual(gateway._usage_since(30), _DAY_29)

    def test_usage_today_goes_through_day_of(self) -> None:
        """`today` 必须调用 `db.day_of`（而不是 `time.strftime`）。

        这条用例不依赖进程时区：无论本机是 UTC 还是 Asia/Shanghai，只要「今天」
        绕开了 `db.day_of` 就红。
        """
        with mock.patch.object(db, 'day_of', return_value='2026-11-28') as patched:
            self.assertEqual(gateway._usage_today(), '2026-11-28')
        patched.assert_called_once_with()


class UsageEndpointTodayTest(unittest.TestCase):
    """`/v1/usage` 的 today 段取 `db.day_of()` 那一行，不是进程时区的「今天」。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'd42.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app)
        self.key = keysvc.create_key('d42')

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _add_usage(self, day: str, requests: int) -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '
            'completion_tokens, credit, realm, cache_hit_tokens, cache_miss_tokens) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (day, self.key['id'], 'glm-5.3-flash', requests, 0, 0, 0, 'cn', 0, 0),
        )

    def test_today_bucket_is_the_day_of_row(self) -> None:
        self._add_usage(_DAY, 3)
        self._add_usage(db.day_of(_FIXED_TS - 86400), 2)
        with mock.patch('time.time', return_value=_FIXED_TS):
            resp = self.client.get(
                '/v1/usage', headers={'Authorization': f"Bearer {self.key['key']}"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body['today']['requests'], 3,
                         'today 没取到 db.day_of 那一行（UTC 16:00Z 之后会取成「昨天」）')
        self.assertEqual(body['last_7d']['requests'], 5)
        self.assertEqual(body['last_30d']['requests'], 5)


#: 子进程里把时钟钉死，打印各日边界的取值。用 replace 而不是 format，
#: 免得 JSON 的花括号被当成占位符。
_SNIPPET = textwrap.dedent(
    """
    import json, sys, time
    sys.path.insert(0, "__ROOT__")
    from server import db
    from server.routers import gateway

    FIXED = __FIXED__
    time.time = lambda: FIXED
    print(json.dumps({
        'tz': time.strftime('%z', time.localtime(0)),
        'day_of': db.day_of(),
        'since_1': gateway._usage_since(1),
        'since_7': gateway._usage_since(7),
        'since_30': gateway._usage_since(30),
        'today': gateway._usage_today(),
    }))
    """
).replace('__ROOT__', ROOT.as_posix()).replace('__FIXED__', str(_FIXED_TS))


class TimezoneInvarianceTest(unittest.TestCase):
    """同一固定时刻，UTC 与 Asia/Shanghai 两种进程时区必须算出同一组日期。"""

    _CANDIDATES = ('UTC', 'Asia/Shanghai', 'UTC-8')

    #: 只探时区是否被强制生效，不碰项目代码 —— 探针失败只说明「这个环境不认 TZ」，
    #: 不能和「被测代码坏了」混为一谈（后者必须红，不能 skip）。
    _PROBE = "import time;print(time.strftime('%z', time.localtime(0)))"

    def _probe(self, tz_value: str) -> str | None:
        try:
            proc = subprocess.run(
                [sys.executable, '-c', self._PROBE],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, 'TZ': tz_value},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _run(self, tz_value: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, '-c', _SNIPPET],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT),
            env={**os.environ, 'TZ': tz_value},
        )

    def test_same_result_in_utc_and_shanghai(self) -> None:
        working = {}
        for tz in self._CANDIDATES:
            offset = self._probe(tz)
            if offset:
                working[tz] = offset
        if len(set(working.values())) < 2:
            self.skipTest(
                '无法强制出两个不同时区（本机的 C 运行时不认 IANA 名，TZ 也没生效）：'
                f'实际只拿到 {sorted(working.values())}；日边界口径已由 in-process '
                '用例覆盖，Linux CI（UTC）本身即失败现场')
        first = None
        for tz in working:
            proc = self._run(tz)
            self.assertEqual(
                proc.returncode, 0,
                f'TZ={tz} 下子进程失败：STDOUT={proc.stdout!r} STDERR={proc.stderr!r}')
            try:
                out = json.loads(proc.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                self.fail(f'TZ={tz} 下没有解析出结果：{proc.stdout!r}')
            self.assertEqual(out['since_1'], _DAY, f'{tz} 下 since_1 错')
            self.assertEqual(out['since_7'], _DAY_6, f'{tz} 下 since_7 错')
            self.assertEqual(out['since_30'], _DAY_29, f'{tz} 下 since_30 错')
            self.assertEqual(out['today'], _DAY, f'{tz} 下 today 错')
            self.assertEqual(out['day_of'], _DAY, f'{tz} 下 day_of 错')
            payload = {k: v for k, v in out.items() if k != 'tz'}
            if first is None:
                first = payload
            else:
                self.assertEqual(payload, first, f'{tz} 与首个时区结果不一致')


if __name__ == '__main__':
    unittest.main()
