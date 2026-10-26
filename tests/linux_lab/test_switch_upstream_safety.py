"""Safety tests for the lab nginx upstream switch controller.

Signals and nginx reload are mocked; one readiness case uses real loopback HTTP.
The nginx.conf
rewrite (set_upstream_ports) is deliberately NOT mocked so that rollback can be
verified against the real bytes on disk.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location('switch_upstream', HERE / 'switch_upstream.py')
su = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault('switch_upstream', su)
_SPEC.loader.exec_module(su)

OLD, NEW = 18081, 18082
OLD_PID, NEW_PID = 4101, 4102

CONF_TEXT = f"""events {{}}
http {{
  {su.MARK_BEGIN}
  upstream manager_backend {{
    server 127.0.0.1:{OLD} max_fails=1 fail_timeout=2s;
    server 127.0.0.1:{NEW} max_fails=1 fail_timeout=2s;
  }}
  {su.MARK_END}
}}
"""


def install(case, readyz, front, reload=None):
    """Install mocks; readyz/front are finite side_effect sequences."""
    def replace(name, value):
        context = patch.object(su, name, value)
        context.start()
        case.addCleanup(context.stop)

    waits = Mock(side_effect=list(readyz))
    fronts = Mock(side_effect=list(front))
    reloads = Mock(side_effect=list(reload) if reload is not None else [None] * 6)
    drains, resumes = Mock(), Mock()
    replace('wait_readyz', waits)
    replace('wait_front_ok', fronts)
    replace('reload_nginx', reloads)
    replace('signal_drain', drains)
    replace('signal_resume', resumes)
    replace('http_readyz', Mock(return_value=200))
    return waits, fronts, reloads, drains, resumes


def run_switch(state, timeline, mode='switch-then-drain'):
    return su.switch(state, timeline, mode, OLD, NEW, 0.1, 0.001)


def event_names(timeline):
    return [call.args[0] for call in timeline.emit.call_args_list if call.args]


def event_detail(timeline, name):
    for call in timeline.emit.call_args_list:
        if call.args and call.args[0] == name:
            return call.kwargs
    raise AssertionError(f'event {name} was not emitted')


class SwitchUpstreamSafetyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        conf = Path(temporary.name) / 'nginx.conf'
        conf.write_text(CONF_TEXT, encoding='utf-8')
        self.env = ({'production_connected': False, 'root': temporary.name,
                     'nginx_conf': str(conf), 'host_port': 18443,
                     'instances': [{'port': OLD, 'pid': OLD_PID},
                                   {'port': NEW, 'pid': NEW_PID}]}, conf)

    def test_candidate_not_ready_touches_nothing(self):
        state, conf = self.env
        original = conf.read_bytes()
        waits, fronts, reloads, drains, resumes = install(self, [None], [])
        timeline = Mock()
        with self.assertRaisesRegex(SystemExit, 'candidate not ready'):
            run_switch(state, timeline, 'drain-then-switch')
        assert conf.read_bytes() == original
        reloads.assert_not_called()
        drains.assert_not_called()
        resumes.assert_not_called()
        fronts.assert_not_called()
        assert waits.call_count == 1
        assert event_names(timeline) == ['new_ready_preflight']
        assert event_detail(timeline, 'new_ready_preflight') == {'new_port': NEW, 'ready': False}


    def test_switch_then_drain_front_timeout_rolls_back(self):
        state, conf = self.env
        original = conf.read_bytes()
        waits, fronts, reloads, drains, resumes = install(
            self, [True, True], [None, True])
        timeline = Mock()
        with self.assertRaisesRegex(SystemExit, 'entry not ready after reload'):
            run_switch(state, timeline)
        # Old instance was never drained, but rollback resumed it defensively.
        drains.assert_not_called()
        resumes.assert_called_once_with(OLD_PID)
        assert reloads.call_count == 2
        assert conf.read_bytes() == original
        assert waits.call_args_list[1].args[0] == OLD
        assert waits.call_args_list[1].args[1] == {200}
        assert event_detail(timeline, 'rollback_start') == {'old_port': OLD}
        assert event_detail(timeline, 'rollback_done') == {'ok': True, 'errors': []}


    def test_drain_then_switch_reload_failure_restores_instance_and_config(self):
        state, conf = self.env
        original = conf.read_bytes()
        waits, fronts, reloads, drains, resumes = install(
            self, [True, True, True], [True],
            reload=[SystemExit('reload-boom'), None])
        timeline = Mock()
        with self.assertRaisesRegex(SystemExit, 'reload-boom'):
            run_switch(state, timeline, 'drain-then-switch')
        # Drain happened, the failed reload is the first one, rollback reload is second.
        drains.assert_called_once_with(OLD_PID)
        resumes.assert_called_once_with(OLD_PID)
        assert reloads.call_count == 2
        assert conf.read_bytes() == original
        assert waits.call_args_list[0].args[0] == NEW
        assert waits.call_args_list[1].args[1] == {503}
        assert waits.call_args_list[2].args[1] == {200}
        assert event_detail(timeline, 'rollback_done')['ok'] is True


    def test_rollback_reload_and_probe_failure_reports_not_ok_and_reraises(self):
        state, conf = self.env
        waits, fronts, reloads, drains, resumes = install(
            self, [True, None], [None, None],
            reload=[None, RuntimeError('restore-boom')])
        timeline = Mock()
        with self.assertRaisesRegex(SystemExit, 'entry not ready after reload'):
            run_switch(state, timeline)
        done = event_detail(timeline, 'rollback_done')
        assert done['ok'] is False
        assert any(err.startswith('entry_restore:') for err in done['errors'])
        assert 'old_not_ready' in done['errors']
        assert 'entry_not_ready' in done['errors']
        fronts.assert_any_call(state, 0.1, 0.001)


    def test_success_switch_then_drain_confirms_front_before_draining(self):
        state, conf = self.env
        waits, fronts, reloads, drains, resumes = install(
            self, [True, True], [True, True])
        timeline = Mock()
        result = run_switch(state, timeline)
        names = event_names(timeline)
        assert names.index('t_switch_reload') < names.index('t_front_ok')
        assert names.index('t_front_ok') < names.index('t_drain_signal')
        assert names.index('t_drain_signal') < names.index('t_first_new_success')
        drains.assert_called_once_with(OLD_PID)
        resumes.assert_not_called()
        done = event_detail(timeline, 't_switch_done')
        assert done['mode'] == 'switch-then-drain'
        assert done['old_port'] == OLD and done['new_port'] == NEW
        assert result['front_ok_after_reload'] is True
        assert result['old_readyz_503'] is True


    def test_success_drain_then_switch_drains_before_reload(self):
        state, conf = self.env
        waits, fronts, reloads, drains, resumes = install(
            self, [True, True], [True])
        timeline = Mock()
        result = run_switch(state, timeline, 'drain-then-switch')
        names = event_names(timeline)
        assert names.index('t_drain_signal') < names.index('t_old_readyz_503')
        assert names.index('t_old_readyz_503') < names.index('t_switch_reload')
        assert names.index('t_switch_reload') < names.index('t_first_new_success')
        # Drain-then-switch has no front confirmation before draining.
        assert fronts.call_count == 1
        assert waits.call_args_list[1].args[1] == {503}
        assert result['old_readyz_503'] is True
        assert result['mode'] == 'drain-then-switch'



    def test_real_loopback_candidate_503_keeps_old_ready_and_config_unchanged(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class ReadyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(self.server.ready_status)
                self.end_headers()
                self.wfile.write(b'readiness-only-lab-fixture')

            def log_message(self, *args):
                pass

        def start(status):
            server = ThreadingHTTPServer(('127.0.0.1', 0), ReadyHandler)
            server.ready_status = status
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def close():
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.addCleanup(close)
            return server.server_port

        old_port, new_port = start(200), start(503)
        state, conf = self.env
        state['instances'] = [{'port': old_port, 'pid': OLD_PID},
                              {'port': new_port, 'pid': NEW_PID}]
        original = conf.read_bytes()
        self.assertEqual(su.http_readyz(old_port), 200)
        self.assertEqual(su.http_readyz(new_port), 503)
        for mode in ('drain-then-switch', 'switch-then-drain'):
            with self.subTest(mode=mode), patch.object(su, 'reload_nginx') as reload_mock, \
                    patch.object(su, 'signal_drain') as drain_mock, \
                    patch.object(su, 'signal_resume') as resume_mock:
                with self.assertRaisesRegex(SystemExit, 'candidate not ready'):
                    su.switch(state, Mock(), mode, old_port, new_port, .1, .02)
                self.assertEqual(conf.read_bytes(), original)
                self.assertEqual(su.http_readyz(old_port), 200)
                reload_mock.assert_not_called()
                drain_mock.assert_not_called()
                resume_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
