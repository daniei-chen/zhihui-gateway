"""整机恢复包快照保留策略的回归测试（2026-10-28）。

`recovery_v2.py` 只负责生成与解密校验，**没有保留逻辑**：每跑一次就往
`/root/backups/zhihui-v2/snapshots/` 多放一个约 128MB 的目录，`runtime/` 是内容寻址
所以不重复占空间。截至 2026-10-28 已 1.5G / 2 份快照，磁盘 31G/59G —— 按每天一次的
节奏约 200 天写满，而写满盘会同时打死数据库、回执和发布。

这里钉住四件事：

1. **排序按目录名时间戳，不按 mtime** —— U20 那次被批量改写的 mtime 坑过两次；
2. `latest.json` 引用那一份**永不删**，哪怕它是最旧的；
3. 「最近 N 份 + N 天内 + latest 引用」三条保留规则的并集；
4. 令牌不匹配 / 目录内容变化 / 路径越界，一律拒绝执行。

运行：`python -m pytest server/tests/test_recovery_retention.py -q`
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'deploy'))

import recovery_retention as retention  # noqa: E402


class RetentionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.snapshots = self.root / 'snapshots'
        self.snapshots.mkdir()
        self.latest = self.root / 'latest.json'
        patcher = mock.patch.multiple(
            retention, ROOT=self.root, SNAPSHOTS=self.snapshots, LATEST=self.latest)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def make(self, name: str, size: int = 8) -> pathlib.Path:
        path = self.snapshots / name
        path.mkdir()
        (path / 'snapshot.part-000.age').write_bytes(b'x' * size)
        return path

    def name(self, delta_days: int) -> str:
        return (datetime.now() - timedelta(days=delta_days)).strftime(retention.STAMP_FORMAT)

    def plan(self, **kwargs):
        return retention.build_plan(kwargs.get('keep', 3), kwargs.get('days', 7))


class OrderingTest(RetentionBase):
    def test_sorted_by_name_not_mtime(self):
        old = self.make('20260101-000000')
        new = self.make('20261028-120000')
        # 故意把「更旧」的那个 mtime 改到未来
        future = (datetime.now() + timedelta(days=30)).timestamp()
        import os
        os.utime(old, (future, future))
        dirs = retention.snapshot_dirs()
        self.assertEqual([p.name for p in dirs], ['20260101-000000', '20261028-120000'])
        # 名字最新的必须排最后（"最近 N 份"取末尾）
        self.assertEqual(dirs[-1], new)

    def test_unparseable_name_is_kept_conservatively(self):
        # 让它既不在「最近 N 份」里、也不在「N 天内」，才能验到「无法解析→保守保留」
        self.make('hand-made-copy')
        self.make(self.name(1))
        plan = self.plan(keep=1, days=0)
        item = next(i for i in plan['items'] if i['path'].endswith('hand-made-copy'))
        self.assertEqual(item['action'], 'keep')
        self.assertIn('无法判断新旧', item['why'])


class RetentionRuleTest(RetentionBase):
    def test_latest_json_reference_never_pruned(self):
        oldest = self.make(self.name(120))
        self.make(self.name(1))
        self.make(self.name(2))
        self.latest.write_text(json.dumps({'snapshot': str(oldest)}))
        plan = self.plan(keep=1, days=0)
        item = next(i for i in plan['items'] if i['path'].endswith(oldest.name))
        self.assertEqual(item['action'], 'keep')
        self.assertIn('latest.json', item['why'])

    def test_recent_n_kept(self):
        for age in (40, 30, 20, 10):
            self.make(self.name(age))
        plan = self.plan(keep=3, days=0)
        pruned = [i for i in plan['items'] if i['action'] == 'prune']
        self.assertEqual(plan['prune_count'], 1)
        self.assertTrue(pruned[0]['path'].endswith(self.name(40)))

    def test_within_days_kept_even_if_beyond_keep_count(self):
        for age in (30, 6, 5, 4, 3, 2, 1):
            self.make(self.name(age))
        plan = self.plan(keep=1, days=7)
        self.assertEqual(plan['prune_count'], 1)   # 只有 30 天那份该走

    def test_latest_json_missing_is_not_fatal(self):
        self.make(self.name(1))
        plan = self.plan()
        self.assertIsNone(plan['latest_snapshot'])
        self.assertEqual(plan['prune_count'], 0)

    def test_latest_json_garbage_is_not_fatal(self):
        self.latest.write_text('not json at all')
        self.make(self.name(1))
        plan = self.plan()
        self.assertIsNone(plan['latest_snapshot'])


class ApplyGuardTest(RetentionBase):
    def _plan_with_candidate(self):
        for age in (40, 30, 20):
            self.make(self.name(age))
        return self.plan(keep=1, days=0)

    def test_wrong_token_deletes_nothing(self):
        plan = self._plan_with_candidate()
        before = sorted(p.name for p in self.snapshots.iterdir())
        self.assertEqual(retention.apply_plan(plan, 'deadbeefdeadbeef'), 2)
        self.assertEqual(sorted(p.name for p in self.snapshots.iterdir()), before)

    def test_directory_change_between_audit_and_apply_is_refused(self):
        plan = self._plan_with_candidate()
        self.make(self.name(0))          # 审计之后又冒出一个快照
        before = sorted(p.name for p in self.snapshots.iterdir())
        self.assertEqual(retention.apply_plan(plan, plan['confirm_token']), 3)
        self.assertEqual(sorted(p.name for p in self.snapshots.iterdir()), before)

    def test_apply_removes_only_prune_items(self):
        plan = self._plan_with_candidate()
        kept_before = {i['path'] for i in plan['items'] if i['action'] == 'keep'}
        rc = retention.apply_plan(plan, plan['confirm_token'])
        self.assertEqual(rc, 0)
        remaining = {str(p) for p in self.snapshots.iterdir()}
        self.assertEqual(remaining, kept_before)

    def test_empty_plan_returns_4(self):
        self.make(self.name(0))
        plan = self.plan(keep=3, days=7)
        self.assertEqual(plan['prune_count'], 0)
        self.assertEqual(retention.apply_plan(plan, plan['confirm_token']), 4)

    def test_latest_reference_survives_even_if_marked_prune(self):
        """构造一个 latest 指向、但同时满足 prune 条件的场景，确保硬闸门生效。"""
        old = self.make(self.name(40))
        for age in (30, 20):
            self.make(self.name(age))
        self.latest.write_text(json.dumps({'snapshot': old.name}))
        plan = self.plan(keep=1, days=0)
        self.assertEqual(retention.apply_plan(plan, plan['confirm_token']), 0)
        self.assertTrue(old.is_dir(), 'latest.json 引用必须仍然存在')


class ArgumentTest(RetentionBase):
    def test_keep_must_be_positive(self):
        self.assertEqual(retention.main(['--keep', '0']), 2)

    def test_days_must_not_be_negative(self):
        self.assertEqual(retention.main(['--days', '-1']), 2)

    def test_audit_is_default_and_never_deletes(self):
        for age in (40, 30, 20):
            self.make(self.name(age))
        before = sorted(p.name for p in self.snapshots.iterdir())
        self.assertEqual(retention.main(['--keep', '1', '--days', '0']), 0)
        self.assertEqual(sorted(p.name for p in self.snapshots.iterdir()), before)


class PlaintextPurgeTest(RetentionBase):
    """加密后残留的明文 tar 必须能安全清掉，且只有在密文复算通过时才允许删。"""

    def _snapshot(self, name: str, payload: bytes, *, with_meta=True, parts=2,
                  break_hash=False) -> pathlib.Path:
        path = self.make(name)
        chunk = max(1, len(payload) // parts)
        pieces = [payload[i:i + chunk] for i in range(0, len(payload), chunk)]
        entries = []
        for index, piece in enumerate(pieces):
            part = path / ('snapshot.part-%03d.age' % index)
            part.write_bytes(piece)
            entries.append({'bytes': len(piece), 'name': part.name,
                            'sha256': hashlib.sha256(piece).hexdigest()})
        (path / 'snapshot.tar.gz').write_bytes(b'PLAINTEXT-DATABASE-DUMP')
        if with_meta:
            recorded = hashlib.sha256(payload).hexdigest()
            if break_hash:
                recorded = 'f' * 64
            meta = {'cipher_sha256': recorded, 'cipher_bytes': len(payload),
                    'directory': str(path), 'encryption': 'age-X25519',
                    'parts': entries}
            (path / 'snapshot.json').write_text(json.dumps(meta))
        return path

    def test_latest_reference_understands_object_form(self):
        """现场形态：latest.json 的 snapshot 是对象，早期只判字符串会静默失效。"""
        snap = self._snapshot('20261028-135554', b'x' * 64)
        self.latest.write_text(json.dumps({'snapshot': {'directory': str(snap)}}))
        self.assertEqual(retention.latest_snapshot_name(), '20261028-135554')

    def test_cipher_match_ok(self):
        snap = self._snapshot('20261028-010101', b'abcdefgh' * 16)
        ok, detail = retention.cipher_matches(snap, retention.snapshot_meta(snap))
        self.assertTrue(ok, detail)

    def test_cipher_match_detects_corruption(self):
        snap = self._snapshot('20261028-010102', b'abcdefgh' * 16, break_hash=True)
        ok, detail = retention.cipher_matches(snap, retention.snapshot_meta(snap))
        self.assertFalse(ok)
        self.assertIn('不符', detail)

    def test_cipher_match_detects_missing_part(self):
        snap = self._snapshot('20261028-010103', b'abcdefgh' * 16)
        (snap / 'snapshot.part-001.age').unlink()
        ok, detail = retention.cipher_matches(snap, retention.snapshot_meta(snap))
        self.assertFalse(ok)
        self.assertIn('缺少分片', detail)

    def test_plaintext_pruned_only_when_cipher_verified(self):
        good = self._snapshot('20261028-010104', b'q' * 128)
        bad = self._snapshot('20261028-010105', b'q' * 128, break_hash=True)
        plan = retention.build_plaintext_plan()
        actions = {pathlib.Path(i['path']).parent.name: i['action'] for i in plan['items']}
        self.assertEqual(actions['20261028-010104'], 'prune')
        self.assertEqual(actions['20261028-010105'], 'keep')
        self.assertEqual(plan['prune_count'], 1)

    def test_missing_meta_keeps_plaintext(self):
        snap = self._snapshot('20261028-010106', b'q' * 64, with_meta=False)
        plan = retention.build_plaintext_plan()
        item = next(i for i in plan['items'] if pathlib.Path(i['path']).parent.name == snap.name)
        self.assertEqual(item['action'], 'keep')
        self.assertIn('无法校验密文', item['why'])

    def test_apply_removes_only_plaintext(self):
        snap = self._snapshot('20261028-010107', b'q' * 128)
        plan = retention.build_plaintext_plan()
        self.assertEqual(retention.apply_plaintext_plan(plan, plan['confirm_token']), 0)
        remaining = {p.name for p in snap.iterdir()}
        self.assertNotIn('snapshot.tar.gz', remaining)
        self.assertIn('snapshot.json', remaining)
        self.assertIn('snapshot.part-000.age', remaining)

    def test_apply_wrong_token_keeps_everything(self):
        snap = self._snapshot('20261028-010108', b'q' * 128)
        plan = retention.build_plaintext_plan()
        self.assertEqual(retention.apply_plaintext_plan(plan, 'wrong-token-here'), 2)
        self.assertTrue((snap / 'snapshot.tar.gz').is_file())

    def test_apply_refuses_when_cipher_broken_after_audit(self):
        snap = self._snapshot('20261028-010109', b'q' * 128)
        plan = retention.build_plaintext_plan()
        self.assertEqual(plan['prune_count'], 1)
        # 审计之后密文被破坏
        (snap / 'snapshot.part-000.age').write_bytes(b'corrupted')
        self.assertEqual(retention.apply_plaintext_plan(plan, plan['confirm_token']), 4)
        self.assertTrue((snap / 'snapshot.tar.gz').is_file())

    def test_no_plaintext_reports_nothing(self):
        snap = self._snapshot('20261028-010110', b'q' * 64)
        (snap / 'snapshot.tar.gz').unlink()
        plan = retention.build_plaintext_plan()
        self.assertEqual(plan['prune_count'], 0)


class AutoModeTest(RetentionBase):
    """无人值守模式：跳过令牌，但安全闸门一个都不能少。"""

    def test_auto_requires_an_action(self):
        self.assertEqual(retention.main(['--auto']), 2)

    def test_auto_apply_prunes_without_token(self):
        for age in (40, 30, 20):
            self.make(self.name(age))
        self.assertEqual(retention.main(['--keep', '1', '--days', '0', '--apply', '--auto']), 0)
        self.assertEqual(len(sorted(self.snapshots.iterdir())), 1)

    def test_auto_apply_still_protects_latest_reference(self):
        old = self.make(self.name(40))
        for age in (30, 20):
            self.make(self.name(age))
        self.latest.write_text(json.dumps({'snapshot': {'directory': str(old)}}))
        self.assertEqual(retention.main(['--keep', '1', '--days', '0', '--apply', '--auto']), 0)
        self.assertTrue(old.is_dir(), 'auto 模式也必须保住 latest.json 引用')

    def test_auto_purge_plaintext_without_token(self):
        snap = self.make('20261028-020202')
        payload = b'z' * 64
        (snap / 'snapshot.part-000.age').write_bytes(payload)
        (snap / 'snapshot.tar.gz').write_bytes(b'PLAINTEXT')
        (snap / 'snapshot.json').write_text(json.dumps({
            'cipher_sha256': hashlib.sha256(payload).hexdigest(),
            'parts': [{'name': 'snapshot.part-000.age', 'bytes': len(payload)}]}))
        self.assertEqual(retention.main(['--purge-plaintext', '--auto']), 0)
        self.assertFalse((snap / 'snapshot.tar.gz').exists())

    def test_auto_purge_refuses_when_cipher_broken(self):
        snap = self.make('20261028-020203')
        (snap / 'snapshot.part-000.age').write_bytes(b'z' * 64)
        (snap / 'snapshot.tar.gz').write_bytes(b'PLAINTEXT')
        (snap / 'snapshot.json').write_text(json.dumps({
            'cipher_sha256': 'f' * 64,
            'parts': [{'name': 'snapshot.part-000.age', 'bytes': 64}]}))
        self.assertEqual(retention.main(['--purge-plaintext', '--auto']), 4)
        self.assertTrue((snap / 'snapshot.tar.gz').exists())


class PlaintextAllowlistTest(RetentionBase):
    """回归：只清已知明文包，绝不碰 sidecar / 签名 / 未知文件。

    2026-10-28 事故：`--purge-plaintext --auto` 用「除 .age 和 snapshot.json 之外
    都算明文」的判据，把 `recovery-manifest.json`（`--publish-prepared` 依赖它）
    和 `metadata.sig` 一起删了。
    """

    def _ready(self, name: str) -> pathlib.Path:
        snap = self.make(name)
        payload = b'p' * 32
        (snap / 'snapshot.part-000.age').write_bytes(payload)
        (snap / 'snapshot.tar.gz').write_bytes(b'PLAINTEXT')
        (snap / 'snapshot.json').write_text(json.dumps({
            'cipher_sha256': hashlib.sha256(payload).hexdigest(),
            'parts': [{'name': 'snapshot.part-000.age', 'bytes': len(payload)}]}))
        return snap

    def test_sidecar_and_signature_are_not_candidates(self):
        snap = self._ready('20261028-030301')
        (snap / 'recovery-manifest.json').write_text('{}')
        (snap / 'metadata.sig').write_bytes(b'sig')
        plan = retention.build_plaintext_plan()
        listed = [f for i in plan['items'] for f in i['files']]
        self.assertTrue(all(pathlib.Path(f).name == 'snapshot.tar.gz' for f in listed), listed)
        retention.apply_plaintext_plan(plan, plan['confirm_token'])
        self.assertTrue((snap / 'recovery-manifest.json').is_file())
        self.assertTrue((snap / 'metadata.sig').is_file())
        self.assertFalse((snap / 'snapshot.tar.gz').exists())

    def test_unknown_file_is_not_a_candidate(self):
        snap = self._ready('20261028-030302')
        (snap / 'notes.txt').write_text('operator note')
        plan = retention.build_plaintext_plan()
        listed = [pathlib.Path(f).name for i in plan['items'] for f in i['files']]
        self.assertNotIn('notes.txt', listed)
        retention.apply_plaintext_plan(plan, plan['confirm_token'])
        self.assertTrue((snap / 'notes.txt').is_file())

    def test_only_tar_names_are_plaintext(self):
        self.assertEqual(
            sorted(retention.PLAINTEXT_NAMES),
            ['runtime.tar', 'runtime.tar.gz', 'snapshot.tar', 'snapshot.tar.gz'])
        self.assertIn('recovery-manifest.json', retention.PROTECTED_NAMES)
        self.assertIn('metadata.sig', retention.PROTECTED_NAMES)


if __name__ == '__main__':
    unittest.main()
