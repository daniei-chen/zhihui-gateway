"""复核补的测试：恢复的作用域、两种跳过原因、连接超时。

这三条都来自维护者复核：第一条钉住一个**容易被「好心改坏」**的行为（镜像里没有的
表不能清空），第二条与第三条是复核时改掉的两处（日志口径 / 连接超时）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest import mock

from server import config, db
from server.services import pgsync

from .test_pgsync import _Case  # 复用同一套临时 HOME/DB 装置

_ROOT = Path(__file__).resolve().parents[2]


class _StubPg:
    """把 PG 连接换成桩：只记录 connect 的参数，并提供一个空的上下文管理器。"""

    def __init__(self, kwargs_sink: list[dict]):
        self._sink = kwargs_sink

    def connect(self, dsn, **kwargs):  # noqa: ANN001
        self._sink.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # 导出流程会在事务里调它们（桩只要不炸即可）。cursor() 给一个「查什么都没有」
    # 的假游标：`_prev_tables` 会因此走「按首次导出处理」那条分支（正是真实首次
    # 导出的形态），而不是抛出一个被它自己吞掉的异常、在测试输出里留一段吓人的栈。
    def commit(self):
        pass

    def rollback(self):
        pass

    def cursor(self):
        return _StubCursor()


class _StubCursor:
    def execute(self, *a, **kw):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class QuarantinedImportTest(_Case):
    """A main-only PG mirror is an artifact, never an online ledger rollback."""

    def _snapshot(self):
        conn=db.connect()
        result={}
        for table in pgsync._local_tables(conn):
            cols=[{'name':r[1],'type':r[2],'notnull':r[3],'pk':r[5]}
                  for r in conn.execute('PRAGMA table_info("'+table+'")')]
            rows=[tuple(r) for r in conn.execute('SELECT * FROM "'+table+'"')]
            result[table]={'columns':cols,'rows':rows}
        return result

    def _import(self, tables, *, rows_factory=None, counts=None):
        counts=counts if counts is not None else {t:len(v['rows']) for t,v in tables.items()}
        class Source:
            def __init__(self,pg):pass
            def tables(self):return list(tables)
            def columns(self,t):return tables[t]['columns']
            def rows(self,t,columns,batch=pgsync.BATCH):
                if rows_factory and t=='restore_probe':yield from rows_factory()
                else:yield tables[t]['rows']
            def close(self):pass
        class Pg(_StubPg):
            def cursor(self):
                class Cursor(_StubCursor):
                    def fetchone(self):return (json.dumps(counts),)
                return Cursor()
        reporter=type('Reporter',(),{'data':{},'log':lambda *a,**k:None,
                                   'step':lambda *a,**k:None})()
        with mock.patch.object(pgsync,'_PgSource',Source), \
             mock.patch.object(pgsync,'_require_psycopg',lambda:Pg([])), \
             mock.patch.object(db,'replace_database',side_effect=AssertionError('online replacement forbidden')):
            return pgsync.import_from_pg({'host':'stub','port':5432,'dbname':'x',
                'user':'u','password':'synthetic','sslmode':'prefer'},reporter)

    def test_incomplete_mirror_is_rejected_without_changing_online_rows(self):
        db.execute("INSERT INTO settings VALUES('live-sentinel','new')")
        with self.assertRaisesRegex(ValueError,'缺少当前数据表'):
            self._import({})
        self.assertEqual(db.query_one("SELECT value FROM settings WHERE key='live-sentinel'")['value'],'new')

    def test_complete_mirror_is_new_quarantined_artifact_with_immutable_triggers(self):
        db.execute("INSERT INTO settings VALUES('live-sentinel','old')")
        tables=self._snapshot()
        db.execute("UPDATE settings SET value='new' WHERE key='live-sentinel'")
        result=self._import(tables)
        self.assertFalse(result['live_database_replaced'])
        self.assertTrue(result['activation_quarantined'])
        self.assertEqual(db.query_one("SELECT value FROM settings WHERE key='live-sentinel'")['value'],'new')
        staged=sqlite3.connect(Path(config.DATA_DIR)/result['artifact'])
        try:
            self.assertEqual(staged.execute("SELECT value FROM settings WHERE key='live-sentinel'").fetchone()[0],'old')
            self.assertTrue(staged.execute('SELECT reason FROM _wb_restore_quarantine').fetchone())
            triggers=staged.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='admin_events'").fetchall()
            self.assertGreaterEqual(len(triggers),2)
        finally:staged.close()

    def test_row_receipt_mismatch_cleans_artifact_and_preserves_online_data(self):
        tables=self._snapshot()
        counts={t:len(v['rows']) for t,v in tables.items()};counts['settings']+=1
        before=set(Path(config.DATA_DIR).glob('.pgsync-restore-*'))
        with self.assertRaisesRegex(ValueError,'行数回执不匹配'):
            self._import(tables,counts=counts)
        self.assertEqual(set(Path(config.DATA_DIR).glob('.pgsync-restore-*')),before)

    def test_online_read_and_write_continue_during_materialization(self):
        db.execute('CREATE TABLE restore_probe(id INTEGER PRIMARY KEY,v TEXT)')
        db.execute("INSERT INTO restore_probe VALUES(1,'old')")
        tables=self._snapshot()
        gate=threading.Event()
        def rows():
            def probe():
                db.execute("UPDATE restore_probe SET v='new' WHERE id=1")
                gate.set()
            worker=threading.Thread(target=probe)
            worker.start()
            self.assertTrue(gate.wait(3),'copy blocked online writes')
            worker.join(3)
            yield [(1,'old')]
        result=self._import(tables,rows_factory=rows)
        self.assertEqual(db.query_one('SELECT v FROM restore_probe')['v'],'new')
        self.assertTrue((Path(config.DATA_DIR)/result['artifact']).exists())

    def test_interrupted_mirror_does_not_change_online_data(self):
        db.execute('CREATE TABLE restore_probe(id INTEGER PRIMARY KEY,v TEXT)')
        db.execute("INSERT INTO restore_probe VALUES(1,'live')")
        def rows():
            yield [(1,'old')]
            raise RuntimeError('synthetic disconnected mirror')
        with self.assertRaisesRegex(RuntimeError,'disconnected'):
            self._import(self._snapshot(),rows_factory=rows)
        self.assertEqual(db.query_one('SELECT v FROM restore_probe')['v'],'live')


class ReplaceDatabaseTest(_Case):
    """`db.replace_database`：换库成功、失败回滚、残留文件清理。"""

    def _staged_copy(self, value: str) -> Path:
        staging = Path(self._tmp.name) / 'staging.db'
        prober = Path(self._tmp.name) / 'probe.db'
        with db._lock:
            db.connect().execute('VACUUM INTO ?', (str(prober),))
        conn = sqlite3.connect(str(prober))
        try:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                         (json.dumps(value),))
            conn.commit()
        finally:
            conn.close()
        os.replace(prober, staging)
        return staging

    def test_swap_replaces_data_and_cleans_temp_files(self) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                        (json.dumps('旧'),))
        staging = self._staged_copy('新')
        db.replace_database(staging)
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'swap_probe'")
        self.assertEqual(json.loads(row['value']), '新')
        self.assertFalse(staging.exists(), '临时库没有被移走')
        self.assertFalse(Path(str(config.DB_PATH) + '.swap-old').exists(),
                         '旧库备份残留')

    def test_swap_failure_restores_old_database(self) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                        (json.dumps('旧'),))
        staging = self._staged_copy('新')
        real_replace = db.os.replace

        def flaky(src, dst):
            if Path(src) == staging:
                raise OSError('磁盘写满了')
            return real_replace(src, dst)

        with mock.patch.object(db.os, 'replace', flaky):
            with self.assertRaises(OSError):
                db.replace_database(staging)
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'swap_probe'")
        self.assertEqual(json.loads(row['value']), '旧', '替换失败后旧库没有被放回来')

    def test_recovers_if_previous_swap_died_between_renames(self) -> None:
        """上次换库死在「旧库已改名、新库还没就位」时，不能把仅存的数据删掉。"""
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('swap_probe', ?)",
                        (json.dumps('旧'),))
        staging = self._staged_copy('新')
        target = Path(config.DB_PATH)
        old = Path(str(target) + '.swap-old')

        # 模拟崩溃现场：连接已关，正式库不在，只剩 .swap-old
        with db._lock:
            db._close_connection_locked()
        os.replace(target, old)
        self.assertFalse(target.exists())

        real_replace = db.os.replace

        def flaky(src, dst):
            if Path(src) == staging:
                raise OSError('磁盘写满了')
            return real_replace(src, dst)

        with mock.patch.object(db.os, 'replace', flaky):
            with self.assertRaises(OSError):
                db.replace_database(staging)
        row = self.db.query_one("SELECT value FROM settings WHERE key = 'swap_probe'")
        self.assertEqual(json.loads(row['value']), '旧',
                         '崩溃残骸被当成垃圾删掉，仅存的数据丢了')


class ValidationOrderTest(_Case):
    """配置校验要**先于**驱动检查。

    复核时发现：`test_connection` 原来先 `_require_psycopg()`。于是没装驱动的
    机器上填了个空地址，用户看到的是「未安装 PostgreSQL 驱动」——而真正要改的是
    地址。顺带这也让「空地址」那条用例在没装驱动的环境里**假通过**（它断言的
    只是 ok=False，走的却是另一条分支）。
    """

    def test_empty_host_is_reported_even_without_the_driver(self) -> None:
        with mock.patch.object(pgsync, '_require_psycopg',
                               side_effect=AssertionError('不该走到要驱动这一步')):
            result = pgsync.test_connection(dict(pgsync.DEFAULT_CONFIG))
        self.assertFalse(result['ok'])
        self.assertIn('地址', result['message'],
                      f'没填地址时给出的不是地址相关的提示：{result["message"]}')


class ConnectTimeoutTest(_Case):
    """导出与恢复的连接都要带 connect_timeout。

    黑洞地址（SYN 被丢）下 libpq 默认挂到 OS 的 TCP 超时（分钟级），期间任务锁
    一直被占着、界面停在「进行中」，用户分不清是网络慢还是卡死。
    """

    def test_both_flows_pass_a_timeout(self) -> None:
        calls: list[dict] = []
        sink = type('S', (), {
            'accepts': lambda self, t, c: list(c),
            'prepare': lambda self, t, c: None,
            'write': lambda self, t, c, r: len(r),
            'drop': lambda self, t: None,
        })()
        rep = type('R', (), {
            'data': {},
            'log': lambda self, *a, **k: None,
            'step': lambda self, *a, **k: None,
        })()
        cfg = {'host': 'stub', 'port': 5432, 'dbname': 'x', 'user': 'u',
               'password': 'p', 'sslmode': 'prefer'}
        with mock.patch.object(pgsync, '_require_psycopg', lambda: _StubPg(calls)), \
             mock.patch.object(pgsync, '_write_meta', lambda pg, tables: None), \
             mock.patch.object(pgsync, '_PgSink', lambda pg: sink):
            pgsync.export_to_pg(cfg, rep)
        self.assertTrue(calls, '导出没有调用 psycopg.connect')
        self.assertEqual(calls[-1].get('connect_timeout'), pgsync.CONNECT_TIMEOUT,
                         f'导出连接没带超时：{calls[-1]}')

class PgSyncPanelPollingTest(unittest.TestCase):
    """issue #122：备份页的轮询不得覆盖正在填的表单。

    报障：填 PG 同步配置时输入框每约 15 秒被服务端旧值打回去 —— 空闲轮询走的是
    **完整重载**，顺带把表单 state 整体重置。这里钉住两件事，都是「只有真人填一遍
    才会发现」的类型：

      ① 轮询（空闲 15 秒 / 运行中 1.5 秒）只拉进度，一次都不碰表单；
      ② 服务端配置回填表单时必须过脏检查（有未保存输入就不覆盖），保存成功后把
         脏标记清掉。

    行为验收在 `dev/verify_pgsync_form_ui.py`（真浏览器里打字等两个轮询周期），
    这里钉结构：改回「空闲也全量重载」会立刻被拦下。轮询已改成请求完成后
    setTimeout，另外检查 single-flight、取消、身份代次和可见性；实际组件竞态
    在 web/tests/audit-business-state.test.cjs 执行，不依赖此处的源码形状断言。
    """

    PANEL = (_ROOT / 'web' / 'components' / 'common' / 'settings'
             / 'PgSyncPanel.tsx')

    def setUp(self) -> None:
        raw = self.PANEL.read_text(encoding='utf-8')
        # 断言要检查实际代码，不能让解释旧缺陷的注释提供虚假的匹配。
        self.src = re.sub(r'//[^\n]*', '', re.sub(r'/\*.*?\*/', '', raw, flags=re.S))
        self.api = (_ROOT / 'web' / 'lib' / 'api.ts').read_text(encoding='utf-8')

    def _polling_contract(self, src: str) -> None:
        effect = re.search(
            r'^  useEffect\(\(\) => \{\s*(let alive = true;[\s\S]*?)'
            r'^  \}, \[([^\]]*)\]\);', src, flags=re.M)
        self.assertIsNotNone(effect, '找不到独立进度轮询的生命周期')
        body, deps = effect.groups()
        for dep in ('acceptStatus', 'isAdmin', 'me?.username', 'invalidateStatus'):
            self.assertIn(dep, deps, f'轮询缺少当前身份/状态依赖 {dep}')
        tick = re.search(
            r'^    const tick = async \(\) => \{\n([\s\S]*?)^    \};', body, flags=re.M)
        self.assertIsNotNone(tick, '找不到异步进度读取')
        request = tick.group(1)
        self.assertIn('await pgSyncApi.status(request.signal)', request,
                      '轮询必须把取消 signal 传入进度 API')
        self.assertNotRegex(request, r'\bload\s*\(|\bsetForm\s*\(',
                            '轮询又出现完整重载或表单更新 —— issue #122 会覆盖未保存输入')
        self.assertRegex(request, r'if \(!alive \|\| document\.hidden \|\| inFlight\) return;',
                         '轮询没有同步在途/可见性门控')
        self.assertIn('inFlight = true;', request)
        self.assertLess(request.find('inFlight = true;'), request.find('await pgSyncApi.status('),
                        '在途锁必须在发请求之前生效')
        self.assertIn('const request = new AbortController();', request)
        self.assertIn('controller = request;', request)
        self.assertRegex(request,
                         r'if \(alive && !request\.signal\.aborted && generation === statusGeneration\.current\) acceptStatus\(next\)',
                         '已取消或旧代次的进度不能写入当前状态')
        finalizer = request.split('} finally {', 1)
        self.assertEqual(len(finalizer), 2, '请求完成/失败都必须释放锁并安排下一次轮询')
        self.assertIn('inFlight = false;', finalizer[1])
        self.assertIn('else schedule();', finalizer[1])
        self.assertRegex(body,
                         r'if \(alive && !document\.hidden\) timer = window\.setTimeout\(\(\) => void tick\(\), runningRef\.current \? 1500 : 15000\)',
                         '完成后轮询必须保留运行 1.5 秒/空闲 15 秒的有界间隔')
        self.assertNotIn('setInterval(', body, '固定 interval 会堆叠慢请求')
        self.assertIn('window.clearTimeout(timer)', body, 'clearTimer 必须实际撤掉计时器')
        visibility = re.search(
            r'^    const visibility = \(\) => \{\n([\s\S]*?)^    \};', body, flags=re.M)
        self.assertIsNotNone(visibility, '缺少标签页隐藏/恢复处理')
        self.assertIn('if (document.hidden)', visibility.group(1))
        self.assertIn('controller?.abort();', visibility.group(1))
        self.assertIn('statusGeneration.current++;', visibility.group(1))
        self.assertIn('else if (inFlight) resumeImmediately = true;', visibility.group(1))
        self.assertIn('else void tick();', visibility.group(1))
        cleanup = re.search(
            r'^    return \(\) => \{\n([\s\S]*?)^    \};', body, flags=re.M)
        self.assertIsNotNone(cleanup, '离开轮询作用域必须取消与清理')
        for action in ('alive = false;', 'invalidateStatus();', 'clearTimer();',
                       'controller?.abort();', "document.removeEventListener('visibilitychange', visibility)"):
            self.assertIn(action, cleanup.group(1), f'轮询清理缺少 {action}')
        self.assertRegex(src, r'if \(wasRunning\.current && !running\) void load\(\);',
                         '任务完成时仍需刷新上次导入/导出时间')

    def test_轮询回调只拉进度(self) -> None:
        self._polling_contract(self.src)
        self.assertRegex(self.api,
                         r"status: \(signal\?: AbortSignal\) => get<PgSyncStatus>\('/api/settings/pg-sync/status', undefined, signal\)",
                         '进度 API 必须把 signal 继续传给 HTTP 层')

    def test_回填表单要过脏检查(self) -> None:
        loader = re.search(
            r'^  const load = useCallback\(async \(\) => \{\n([\s\S]*?)'
            r'^  \}, \[([^\]]*)\]\);', self.src, flags=re.M)
        self.assertIsNotNone(loader, '找不到配置加载函数')
        body = loader.group(1)
        self.assertIn('dirtyRef.current', body,
                      '回填表单没有脏检查：用户没保存的输入会被旧值打回去')
        self.assertIn('setForm((f) =>', body,
                      '回填要走函数式更新（基于当前表单判断，而不是闭包里的旧值）')
        self.assertIn('acceptStatus', loader.group(2), '配置读取必须跟随状态提交回调的依赖')
        self.assertIn('await pgSyncApi.config(controller.signal)', body)
        self.assertIn('mounted.current && !controller.signal.aborted && generation === configGeneration.current', body,
                      '旧身份/已取消的配置读取不能回填表单')
        self.assertIn('if (!current()) return;', body)
        self.assertLess(body.find('if (!current()) return;'), body.find('setForm((f) =>'),
                        '配置读取应在回填前拒绝旧代次')
        self.assertIn('if (statusRead === statusGeneration.current) acceptStatus(r.status)', body)
        self.assertRegex(self.api,
                         r"config: \(signal\?: AbortSignal\) => get<PgSyncConfigResponse>\('/api/settings/pg-sync', undefined, signal\)")

    def test_脏标记的置位与清零(self) -> None:
        self.assertRegex(self.src, r'const patch = \(next[\s\S]{0,160}?dirtyRef\.current = true',
                         'patch() 没置脏标记 —— 之后任何回填都会覆盖用户输入')
        self.assertRegex(self.src, r'setForm\(r\.config\);\n\s*dirtyRef\.current = false',
                         '保存成功后没清脏标记 —— 之后服务端值再也回填不进表单')

    def test_守卫不是空转(self) -> None:
        """实际变异轮询实现：恢复整表读取、移除互斥/取消/身份边界都必须失败。"""
        mutations = (
            ('const next = await pgSyncApi.status(request.signal);',
             'await load();\n        const next = await pgSyncApi.status(request.signal);'),
            ('!alive || document.hidden || inFlight', '!alive || document.hidden'),
            ('alive && !request.signal.aborted && generation', 'alive && generation'),
            ('[acceptStatus, isAdmin, me?.username, invalidateStatus]',
             '[acceptStatus, isAdmin, invalidateStatus]'),
        )
        for old, new in mutations:
            with self.subTest(removed_contract=old):
                broken = self.src.replace(old, new, 1)
                self.assertTrue(broken != self.src, '替换没生效，这条反证没有意义')
                with self.assertRaises(AssertionError):
                    self._polling_contract(broken)


if __name__ == '__main__':
    unittest.main()
