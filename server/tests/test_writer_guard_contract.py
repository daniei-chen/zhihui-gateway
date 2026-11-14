"""The fixed host writer profile must actually expose the drain contract."""
from pathlib import Path
import hashlib
import re
from contextlib import contextmanager
from types import SimpleNamespace
import sys
import unittest
from unittest import mock


OPS = Path(__file__).resolve().parents[2] / "ops" / "writer-guard"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import host_adapter  # noqa: E402
import launch_guard  # noqa: E402
import migration  # noqa: E402
import service_gate  # noqa: E402
import systemd_adapter  # noqa: E402
from launch_guard import writer_command, writer_environment  # noqa: E402


class WriterGuardContractTests(unittest.TestCase):
    def test_writer_uses_managed_drain_launcher_and_shared_admission(self):
        release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
        command = writer_command()
        self.assertEqual(command[command.index("-m") + 1], "server.serve")
        self.assertIn("--timeout-graceful-shutdown", command)
        environment = writer_environment({}, release)
        self.assertEqual(environment["WB_SHARED_ADMISSION"], "1")

    @unittest.skipUnless(hasattr(launch_guard.os, 'chown'), 'POSIX ownership contract only')
    def test_service_data_contract_normalizes_private_handoff_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            users = root / 'users.json'
            password = root / 'initial-admin-password.txt'
            users.write_text('{}', encoding='utf-8')
            password.write_text('secret', encoding='utf-8')
            users.chmod(0o644)
            password.chmod(0o644)
            root.chmod(0o755)
            launch_guard.normalize_service_data(
                root, uid=launch_guard.os.getuid(), gid=launch_guard.os.getgid())
            self.assertEqual(launch_guard.stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(launch_guard.stat.S_IMODE(users.stat().st_mode), 0o600)
            self.assertEqual(launch_guard.stat.S_IMODE(password.stat().st_mode), 0o600)


    def test_helper_keeps_shared_admission_but_disables_background_jobs(self):
        release = SimpleNamespace(root=Path("/opt/workbuddy-manager/releases/candidate"))
        command = writer_command(helper=True)
        self.assertEqual(command[command.index("--port") + 1], "7865")
        environment = writer_environment({}, release, helper=True)
        self.assertEqual(environment["WB_SHARED_ADMISSION"], "1")
        self.assertEqual(environment["WB_BACKGROUND_JOBS"], "0")

    def test_helper_is_a_real_guarded_systemd_profile(self):
        self.assertEqual(systemd_adapter.entry_command(systemd_adapter.HELPER)[-1], 'helper')
        self.assertIn('workbuddy-web-helper.service', service_gate.UNITS)
        self.assertIn('writer_entry.py helper',
                      systemd_adapter.launch_dropin(systemd_adapter.HELPER).decode())
        helper_unit = (Path(__file__).parents[2] / 'deploy' /
                       'workbuddy-web-helper.service').read_text(encoding='utf-8')
        self.assertNotIn('Requires=workbuddy-web.service', helper_unit)

    def test_systemd_loads_an_inactive_helper_before_property_reads(self):
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=(b'{"type":"o","data":['
                        b'"/org/freedesktop/systemd1/unit/'
                        b'workbuddy_2dweb_2dhelper_2eservice"]}'),
            )

        adapter = systemd_adapter.Systemd(runner=runner)
        path = adapter.object_path(systemd_adapter.HELPER)
        self.assertEqual(path, '/org/freedesktop/systemd1/unit/workbuddy_2dweb_2dhelper_2eservice')
        self.assertIn('LoadUnit', calls[0])
        self.assertNotIn('GetUnit', calls[0])

    def test_maintenance_fence_leaves_exact_relay_roots_available(self):
        maintenance_source = (Path(__file__).parents[2] / 'ops' /
                              'container-maintenance' / 'maintenance.py').read_text(encoding='utf-8')
        rule = next(line.split('=', 1)[1].strip() for line in maintenance_source.splitlines()
                    if line.startswith('PUBLIC_RULE = '))
        self.assertNotIn('|v1|', rule)
        self.assertIn('(api|qoder|trae', rule)
        self.assertIn('workbuddy', rule)

    def test_migration_entry_binds_the_installed_maintenance_bytes(self):
        entry = (Path(__file__).parents[2] / 'ops' / 'writer-guard' /
                 'migration_entry.py').read_text(encoding='utf-8')
        match = re.search(r"MAINTENANCE_SHA = '([0-9a-f]{64})'", entry)
        self.assertIsNotNone(match)
        maintenance = (Path(__file__).parents[2] / 'ops' /
                       'container-maintenance' / 'maintenance.py').read_bytes()
        normalized = maintenance.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        self.assertEqual(match.group(1), hashlib.sha256(normalized).hexdigest())

    def test_rolling_controller_routes_before_drain_and_retires_helper_last(self):
        class Journal:
            def __init__(self):
                self.record = {'phase': 'prepared', 'mode': 'rolling', 'epoch': 1}

            def save(self, _authority, expected, phase, **_changes):
                self.assert_same(expected)
                self.record = {**expected, 'phase': phase, 'epoch': expected['epoch'] + 1}
                return self.record

            def assert_same(self, value):
                if value is not self.record:
                    raise AssertionError('controller lost journal CAS identity')

            def _authorized(self, _authority):
                return None

            def load(self, _authority):
                return self.record

        class Lock:
            @contextmanager
            def acquire(self, **_kwargs):
                yield object()

            def validate(self, _permit, **_kwargs):
                return None

        class Guard:
            def __init__(self, capability):
                self.capability = capability

            def observe(self, _permit):
                return object()

            def load_floor(self):
                return SimpleNamespace(capabilities=self.capability)

            def require_launch(self, *_args):
                return None

        class Host:
            def __init__(self):
                self.events = []

            def __getattr__(self, name):
                if name in {'point_rolling', 'start_helper', 'helper_ready',
                            'route_helper', 'fence', 'drain_old',
                            'stop_primary_rolling', 'start', 'ready',
                            'route_main', 'stop_helper', 'resume',
                            'verify_complete'}:
                    def effect(*_args):
                        self.events.append(name)
                        # 真实的 `helper_ready` 返回候选进程身份（非 None），其余
                        # effect 返回 None —— 夹具必须与这条合同一致，否则测不出
                        # "把返回身份误当失败" 的回归。
                        return {'pid': 4242} if name == 'helper_ready' else None
                    return effect
                raise AttributeError(name)

        capability = object()
        journal = Journal()
        host = Host()
        controller = migration.Controller.__new__(migration.Controller)
        controller.journal = journal
        controller.lock = Lock()
        controller.guard = Guard(capability)
        controller.host = host
        controller.first_install = None
        controller._release = lambda _record, _name: SimpleNamespace(capabilities=capability)

        result = controller._run_rolling(object(), journal.record, object())
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(host.events, [
            'point_rolling', 'start_helper', 'helper_ready', 'route_helper',
            'fence', 'drain_old', 'stop_primary_rolling', 'start', 'ready',
            'route_main', 'stop_helper', 'resume', 'verify_complete',
        ])


    def test_resume_ready_retries_transient_protected_state_change(self):
        host = host_adapter.Host.__new__(host_adapter.Host)
        expected = object()
        attempts = []
        host.ready_identity = expected

        def flaky_ready(record, candidate):
            attempts.append((record, candidate))
            if len(attempts) == 1:
                raise host_adapter.Rejected("unsafe protected file")
            return expected

        host._ready = flaky_ready
        with mock.patch.object(host_adapter.time, "monotonic", side_effect=[0.0, 0.0]), \
                mock.patch.object(host_adapter.time, "sleep"):
            host._resume_ready("record", "candidate")

        self.assertEqual(len(attempts), 2)

    def test_final_verification_ready_uses_the_same_retry_contract(self):
        host = host_adapter.Host.__new__(host_adapter.Host)
        expected = object()
        attempts = []

        def flaky_ready(record, candidate):
            attempts.append((record, candidate))
            if len(attempts) == 1:
                raise host_adapter.Rejected("observed state changed during inspection")
            return expected

        host._ready = flaky_ready
        with mock.patch.object(host_adapter.time, "monotonic", side_effect=[0.0, 0.0]), \
                mock.patch.object(host_adapter.time, "sleep"):
            self.assertIs(host._retry_ready("record", "candidate", "verify-ready"), expected)

        self.assertEqual(len(attempts), 2)

    def test_read_only_sqlite_sessions_do_not_block_a_migration(self):
        """只读会话既不写库也不持写锁，不该被当成并发写者。

        判据必须是"以可写方式打开/映射"，而不是"打开过"。`-shm` 是 WAL 的协调段，
        只读连接也会打开它，所以单独排除；`-wal` 里有未合并的页，必须算写者。
        """
        source = (Path(__file__).parents[2] / 'ops' / 'writer-guard' /
                  'writer_inventory.py').read_text(encoding='utf-8')
        self.assertIn('def _writable_fd(', source)
        self.assertIn("return bool(int(line.split()[1], 8) & 0o3)", source)
        self.assertIn('def _wal_coordination_only(', source)
        self.assertIn("return target.endswith('-shm')", source)
        self.assertIn("if 'w' not in perms:", source)
        # 打开判定必须走可写性检查，且 -shm 必须在判定前被跳过
        self.assertIn('if _writable_fd(path, fd.name):', source)
        self.assertIn('if _wal_coordination_only(target):', source)
        self.assertIn('if _writable_map(line):', source)

    def test_writer_scan_excludes_the_whole_call_chain(self):
        """迁移不能把"谁在调用我"当成并发写者。

        `sudo` / `bash -c` 的 cwd 可能正落在 `releases/` 下（在同一 shell 里先
        `cd` 到发布目录再跑迁移就是常见操作）。只排除自己的 PID 会让那次迁移
        自锁 —— 实测表现为 `prepare` 第一次必然失败、原样重试才成功。
        """
        source = (Path(__file__).parents[2] / 'ops' / 'writer-guard' /
                  'writer_inventory.py').read_text(encoding='utf-8')
        self.assertIn('ancestors = set()', source)
        self.assertIn('int(path.name) in ancestors', source)
        # 旧写法（只排除自己）必须已经不存在
        self.assertNotIn('int(path.name) == os.getpid()', source)

    def test_identical_console_build_may_be_reused_by_a_backend_only_release(self):
        """纯后端发版不该被迫重建前端：同名同内容放行，同名不同内容才拒绝。"""
        source = (Path(__file__).parents[2] / 'ops' / 'writer-guard' /
                  'frontend_adapter.py').read_text(encoding='utf-8')
        self.assertIn('if build == current.name:', source)
        guard = "if self.auth.tree(candidate.root / 'web/out') != self.auth.tree(current):"
        self.assertIn(guard, source)
        # 守卫必须紧跟在同名分支里，且仍然保留原来的拒绝信息
        block = source[source.index(guard):]
        self.assertIn("raise Rejected('new console build must not overwrite existing build')",
                      block[:400])

    def test_rolling_helper_readiness_is_not_treated_as_a_none_effect(self):
        """`helper_ready` 的成功结果是候选进程身份，不是 None。

        把它交给 `_effect`（"call 必须返回 None 才算完成"）会在 helper 已经
        确认就绪之后抛出 `host effects must verify completion or raise`，把一次
        成功的切换判成失败并立刻围栏。这条合同锁住"直接校验返回值"的写法。
        """
        source = (Path(__file__).parents[2] / 'ops' / 'writer-guard' /
                  'migration.py').read_text(encoding='utf-8')
        self.assertNotIn('_effect(self.host.helper_ready', source)
        self.assertIn('if self.host.helper_ready(record, candidate) is None:', source)
        self.assertIn('helper readiness returned no process identity', source)
