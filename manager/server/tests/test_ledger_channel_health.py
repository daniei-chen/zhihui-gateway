"""N01/N02 回归：死信纳入持久健康判定；回执通道失败进入新请求准入。

复审报告 `reaudit-20261028-1108` 的两个复现，这里各钉成断言：

**N01（死信未纳入健康判定）**：5 次 apply 失败把回执移进 `dead/` 之后，
三轮**空重放**就让 `readiness.ok=true` —— 因为健康只看 `spool/*.json` 的条数。
现在 `dead/` 里的**未处置**死信是持久状态：跨轮、跨重启都算降级，只有带
actor+reason 的处置（留痕在 `dead/dispositions.jsonl`）才能消账。

**N02（通道失败未进入准入）**：把 spool 指向**不可创建**的目录 →
`spill()` 返回 None，而 `should_backpressure()` 仍为 False（`backlog()` 把 OSError
当成 0）→ 新请求照收；三轮无文件重放又把 `spool_failed` 清掉、`readiness` 恢复。
现在准入看通道状态（真实写探测 + 死信），恢复必须由写探测成功证明。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import (budget, budgetowner, config, db, keysvc, ledgerreplay,  # noqa: E402
                    observability, operational)


class _ChannelFixture(unittest.TestCase):
    """临时库 + 临时 spool + 一个密钥；并保存/恢复 operational 的全局标志。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self._orig_data = config.DATA_DIR
        self._orig_spool = os.environ.get('WB_LEDGER_SPOOL_DIR')
        self._orig_backpressure = os.environ.get('WB_LEDGER_BACKPRESSURE')
        self._orig_healthy = operational.ledger_write_healthy
        self._orig_spool_failed = operational.ledger_spool_failed
        self._orig_streak = operational._ledger_replay_streak
        self._orig_alerts = dict(operational.alerts)
        root = pathlib.Path(self._tmp.name)
        config.DATA_DIR = root
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        self.spool = root / 'spool'
        os.environ['WB_LEDGER_SPOOL_DIR'] = str(self.spool)
        os.environ.pop('WB_LEDGER_BACKPRESSURE', None)
        db._conn = None
        db.connect()
        budget.initialize()
        operational.ledger_write_healthy = True
        operational.ledger_spool_failed = False
        operational._ledger_replay_streak = 0
        operational.alerts.clear()
        # 「通道被用过」的标记落在 DATA_DIR（跨 spool 目录变更保留），
        # 每个用例都要从「从未用过」开始，否则 absent/ok 的语义会互相污染。
        self._drop_used_marker()
        created = keysvc.create_key(name='n01', realm='cn')
        self.key = keysvc.resolve(created['key'])

    def _drop_used_marker(self) -> None:
        try:
            ledgerreplay._used_marker().unlink()
        except OSError:
            pass

    def tearDown(self) -> None:
        budgetowner.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._drop_used_marker()
        config.DATA_DIR = self._orig_data
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        if self._orig_spool is None:
            os.environ.pop('WB_LEDGER_SPOOL_DIR', None)
        else:
            os.environ['WB_LEDGER_SPOOL_DIR'] = self._orig_spool
        if self._orig_backpressure is None:
            os.environ.pop('WB_LEDGER_BACKPRESSURE', None)
        else:
            os.environ['WB_LEDGER_BACKPRESSURE'] = self._orig_backpressure
        operational.ledger_write_healthy = self._orig_healthy
        operational.ledger_spool_failed = self._orig_spool_failed
        operational._ledger_replay_streak = self._orig_streak
        operational.alerts.clear()
        operational.alerts.update(self._orig_alerts)
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _facts(self, request_id: str = 'req-1') -> dict:
        return {
            'ts': int(time.time()), 'key_id': self.key['id'], 'ip': '127.0.0.1',
            'model': 'fake-model', 'mapped_model': 'fake-model', 'status': 200,
            'prompt_tokens': 10, 'completion_tokens': 5, 'latency_ms': 100,
            'first_token_ms': 20, 'ua': 'pytest', 'error': None, 'stream': 0,
            'credit': 0.5, 'realm': 'cn', 'account': None,
            'cache_hit_tokens': 0, 'cache_miss_tokens': 0, 'cache_write_tokens': 0,
            'request_id': request_id, 'endpoint': '/v1/chat/completions',
            'method': 'POST', 'protocol': 'openai', 'http_status': 200,
            'terminal_state': 'completed', 'error_stage': None,
            'release_id': 'test-release', 'usage_source': 'upstream',
            'usage_complete': 1, 'first_byte_ms': 30, 'platform': 'workbuddy',
            'actual_platform': 'workbuddy', 'upstream_ref': 'default',
            'client_request_id': None, 'effort': None, 'channel': 'api',
            'session_key': None,
        }

    def _spill(self, request_id: str = 'req-1') -> str:
        entry = ledgerreplay.build_entry(self._facts(request_id), release_id='test-release')
        path = ledgerreplay.spill(entry)
        self.assertIsNotNone(path, 'spill 应写入成功')
        return path

    def _make_dead_letter(self, request_id: str = 'req-dead') -> None:
        """让一条回执经历 MAX_ATTEMPTS 次 apply 失败 → 进 dead/。"""
        self._spill(request_id)
        with mock.patch.object(ledgerreplay, 'apply', side_effect=RuntimeError('db down')):
            for _ in range(ledgerreplay.MAX_ATTEMPTS):
                ledgerreplay.replay_batch()
        self.assertEqual(ledgerreplay.dead_letters(), 1,
                         '达到 MAX_ATTEMPTS 后回执应进 dead/')

    def _set_spool(self, path: pathlib.Path) -> None:
        os.environ['WB_LEDGER_SPOOL_DIR'] = str(path)

    def _prepare(self) -> None:
        state = observability.RequestAudit('n01-fixture', '/v1/chat/completions',
                                           'POST', 'openai', time.monotonic())
        token = observability.current.set(state)
        try:
            budget.prepare(dict(db.query_one('SELECT * FROM api_keys WHERE id=?',
                                             (self.key['id'],))), {'messages': []})
        finally:
            observability.current.reset(token)


# ────────────────────────────────────────────────────────────────────────
# N01：死信是持久状态
# ────────────────────────────────────────────────────────────────────────
class DeadLetterPersistenceTest(_ChannelFixture):
    def test_dead_letter_keeps_readiness_degraded_across_empty_rounds(self) -> None:
        self._make_dead_letter()
        for round_index in range(3):
            result = operational.replay_ledger_receipts(int(time.time()))
            self.assertEqual(result['scanned'], 0,
                             f'第 {round_index + 1} 轮应是空轮（没有待重放文件）')
            status = operational.readiness()
            self.assertFalse(status['ok'],
                             'dead/ 里还有未处置死信，三轮空重放也不能恢复健康')
            self.assertEqual(status['ledger_dead'], 1)
            self.assertEqual(status['ledger_channel_state'], 'degraded')
        self.assertIn('ledger_dead_letters', operational.alerts,
                      '未处置死信必须保持告警，不能因空轮消失')

    def test_dead_letter_survives_restart(self) -> None:
        self._make_dead_letter()
        # 模拟重启：进程内标志回到默认值（死信在文件系统上，理应仍然算数）
        operational.ledger_write_healthy = True
        operational.ledger_spool_failed = False
        operational._ledger_replay_streak = 0
        operational.alerts.clear()
        ledgerreplay.recover()          # 启动时的 recover() 只消化存量
        status = operational.readiness()
        self.assertFalse(status['ok'], '重启后 dead/ 仍在，健康必须仍降级')
        self.assertEqual(status['ledger_dead'], 1)

    def test_dispose_requires_actor_and_reason(self) -> None:
        self._make_dead_letter()
        with self.assertRaises(ValueError):
            ledgerreplay.dispose_dead_letters('', 'why')
        with self.assertRaises(ValueError):
            ledgerreplay.dispose_dead_letters('ops', '   ')
        self.assertEqual(ledgerreplay.dead_letters(), 1, '没有处置就不能消账')

    def test_dispose_is_audited_and_restores_health(self) -> None:
        self._make_dead_letter()
        result = ledgerreplay.dispose_dead_letters('ops-u26', '回执重复，已人工核对')
        self.assertEqual(result['disposed'], 1)
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(ledgerreplay.dead_letters(), 0)
        ledger_path = pathlib.Path(result['ledger'])
        self.assertTrue(ledger_path.is_file(), '处置必须留痕')
        record = json.loads(ledger_path.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(record['actor'], 'ops-u26')
        self.assertIn('人工核对', record['reason'])
        self.assertEqual(len(record['sha256']), 64)
        # 文件被移到 processed/（保留证据，不删除）
        self.assertTrue(any((ledgerreplay.dead_dir() / 'processed').glob('*.json')))
        # 处置后仍要经过连续成功轮数才恢复（不是立刻翻真）
        for _ in range(operational._reconcile_env_int('WB_LEDGER_RECOVER_STREAK', 3)):
            operational.replay_ledger_receipts(int(time.time()))
        status = operational.readiness()
        self.assertTrue(status['ok'], f'处置+连续成功轮后应恢复：{status}')
        self.assertEqual(status['ledger_dead'], 0)
        self.assertNotIn('ledger_dead_letters', operational.alerts)


# ────────────────────────────────────────────────────────────────────────
# N02：通道失败进入准入
# ────────────────────────────────────────────────────────────────────────
class ChannelFailureAdmissionTest(_ChannelFixture):
    def _block_spool(self) -> pathlib.Path:
        """把 spool 指到「父路径是普通文件」的位置 → mkdir 必然失败。"""
        blocker = pathlib.Path(self._tmp.name) / 'blocker'
        blocker.write_text('not a directory', encoding='utf-8')
        self._set_spool(blocker / 'spool')
        return blocker

    def test_unwritable_spool_is_detected(self) -> None:
        self._block_spool()
        state = ledgerreplay.channel_state(force_probe=True)
        self.assertFalse(state['writable'], f'写探测应失败：{state}')
        self.assertEqual(state['state'], 'degraded')

    def test_admission_block_reports_stable_code(self) -> None:
        self._block_spool()
        block = ledgerreplay.admission_block()
        self.assertTrue(block['blocked'])
        self.assertEqual(block['code'], ledgerreplay.CODE_SPOOL_UNWRITABLE)

    def test_prepare_rejects_when_spool_unwritable(self) -> None:
        self._block_spool()
        with self.assertRaises(budget.BudgetRejected) as ctx:
            self._prepare()
        self.assertEqual(ctx.exception.code, ledgerreplay.CODE_SPOOL_UNWRITABLE)

    def test_empty_rounds_do_not_clear_spool_failure(self) -> None:
        """空目录不证明能写：spool 不可写时三轮空重放也不能清掉失败标志。"""
        self._block_spool()
        operational.ledger_spool_failed = True
        for _ in range(3):
            operational.replay_ledger_receipts(int(time.time()))
        self.assertTrue(operational.ledger_spool_failed,
                        '空轮不得清除 spool 失败标志')
        self.assertFalse(operational.readiness()['ok'])

    def test_writable_probe_clears_spool_failure(self) -> None:
        """写探测成功才允许恢复（这是唯一依据）。"""
        operational.ledger_spool_failed = True
        self.spool.mkdir(parents=True, exist_ok=True)
        operational.replay_ledger_receipts(int(time.time()))
        self.assertFalse(operational.ledger_spool_failed,
                         '写探测通过后应清除失败标志')

    def test_absent_spool_dir_is_normal(self) -> None:
        """从未用过的目录不存在 = 正常（功能没触发），不能当成降级。"""
        state = ledgerreplay.channel_state(force_probe=True)
        self.assertEqual(state['state'], 'absent')
        self.assertFalse(ledgerreplay.admission_block()['blocked'])
        self._prepare()          # 不抛异常
        self.assertTrue(operational.readiness()['ok'])

    def test_prepare_rejects_while_dead_letters_pending(self) -> None:
        self._make_dead_letter()
        with self.assertRaises(budget.BudgetRejected) as ctx:
            self._prepare()
        self.assertEqual(ctx.exception.code, ledgerreplay.CODE_DEAD_PENDING)
        # 处置之后恢复
        ledgerreplay.dispose_dead_letters('ops', '已人工核对')
        self._prepare()

    def test_backpressure_still_works(self) -> None:
        """积压超限仍然按老语义阻断（code 不变），不是被新逻辑顶掉。"""
        os.environ['WB_LEDGER_BACKPRESSURE'] = '1'
        self.spool.mkdir(parents=True, exist_ok=True)
        self._spill('req-bp')
        block = ledgerreplay.admission_block()
        self.assertTrue(block['blocked'])
        self.assertEqual(block['code'], ledgerreplay.CODE_BACKPRESSURE)
        with self.assertRaises(budget.BudgetRejected) as ctx:
            self._prepare()
        self.assertEqual(ctx.exception.code, 'ledger_backpressure')

    def test_readiness_exposes_dead_and_channel_fields(self) -> None:
        status = operational.readiness()
        for field in ('ledger_dead', 'ledger_dead_dispositions',
                      'ledger_channel_state', 'ledger_channel_writable',
                      'ledger_channel_reason'):
            self.assertIn(field, status, f'就绪输出缺 {field}')


if __name__ == '__main__':
    unittest.main()
