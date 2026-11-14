"""v1.2.86：围栏残留只能从 nginx 配置看出来，而它会让整个管理端消失。

## 背景

2026-11-15 那次迁移失败后，nginx 配置里残留了两行围栏规则：

```nginx
if ($uri ~ ^/(api|qoder|trae|wb|workbuddy|zpush)(/|$)) { return 503; }
```

结果 `/api/*` 全部 503（管理端、登录、工作台全挂），而 `/v1/*` 与 `/healthz`
**完全正常**。

**网关自己察觉不到**：健康检查走进程内逻辑，不经过 nginx。从服务视角看一切正常，
从外面看管理端已经没了。

## 做法

直接读 nginx 配置目录找那个标记，结果作为 `workbuddy_nginx_fence_active` 输出。

**读不到时不输出指标** —— `checked=False` 与 `fenced=False` 是两件完全不同的事，
把「不知道」显示成「没问题」是最糟的选择。
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import main, operational


class FenceDetectionTests(unittest.TestCase):
    def _status(self, directory, files):
        """在临时目录里造出真实配置文件，不 mock 读取逻辑。"""
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in files.items():
                (Path(tmp) / name).write_text(content, encoding='utf-8')
            with mock.patch.object(operational, '_NGINX_CONFIG_PATHS', (tmp,)):
                return operational.nginx_fence_status()

    def test_clean_config_is_not_fenced(self):
        status = self._status('', {'site.conf': 'server { listen 80; }\n'})
        self.assertTrue(status['checked'])
        self.assertFalse(status['fenced'])
        self.assertEqual(status['files'], [])

    def test_marker_is_detected(self):
        content = ('server {\n'
                   '    # zh-container-maintenance-v1\n'
                   '    if ($uri ~ ^/api(/|$)) { return 503; }\n'
                   '}\n')
        status = self._status('', {'site.conf': content})
        self.assertTrue(status['fenced'])
        self.assertEqual(len(status['files']), 1)

    def test_it_reports_which_file_is_fenced(self):
        """要能指出是哪个文件，否则运维还得自己找。"""
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / 'a.conf'
            bad = Path(tmp) / 'b.conf'
            good.write_text('server {}\n', encoding='utf-8')
            bad.write_text('# zh-container-maintenance-v1\n', encoding='utf-8')
            with mock.patch.object(operational, '_NGINX_CONFIG_PATHS', (tmp,)):
                status = operational.nginx_fence_status()
            self.assertEqual(status['files'], [str(bad)])

    def test_missing_directories_mean_not_checked(self):
        """读不到 ≠ 没有围栏。这两件事必须分开。"""
        with mock.patch.object(operational, '_NGINX_CONFIG_PATHS',
                               ('/nonexistent-a', '/nonexistent-b')):
            status = operational.nginx_fence_status()
        self.assertFalse(status['checked'])
        self.assertFalse(status['fenced'],
                         'unknown 时 fenced 应为 False，但 checked 必须是 False')

    def test_it_never_raises_on_unreadable_files(self):
        with mock.patch.object(operational, '_NGINX_CONFIG_PATHS', ('/root',)):
            try:
                operational.nginx_fence_status()
            except Exception as exc:      # noqa: BLE001
                self.fail(f'检测不该抛异常：{exc!r}')

    def test_subdirectories_are_skipped(self):
        """配置目录里可能有备份子目录，不该被当成配置文件读。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'sub').mkdir()
            (Path(tmp) / 'sub' / 'x.conf').write_text(
                '# zh-container-maintenance-v1\n', encoding='utf-8')
            with mock.patch.object(operational, '_NGINX_CONFIG_PATHS', (tmp,)):
                status = operational.nginx_fence_status()
            self.assertFalse(status['fenced'],
                             '子目录里的文件不该被当作生效配置')


class TheMetricIsHonestTests(unittest.TestCase):
    SOURCE = inspect.getsource(main.prometheus_metrics)

    def test_it_only_emits_when_checked(self):
        start = self.SOURCE.index('workbuddy_nginx_fence_active')
        window = self.SOURCE[max(0, start - 400):start + 100]
        self.assertIn("if _fence['checked']", window,
                      '读不到时必须不输出 —— 假 0 会被读成「确认没有围栏」')

    def test_the_help_says_what_happens_when_it_is_one(self):
        start = self.SOURCE.index('workbuddy_nginx_fence_active')
        window = self.SOURCE[start:start + 600]
        self.assertIn('503', window,
                      '要说明为 1 时 /api/* 在返 503，否则看不出严重性')

    def test_it_mentions_that_the_service_looks_fine(self):
        """这是它值得存在的理由：服务自身察觉不到。"""
        start = self.SOURCE.index('workbuddy_nginx_fence_active')
        window = self.SOURCE[start:start + 600]
        self.assertIn('healthy', window)

    def test_errors_do_not_break_metrics(self):
        start = self.SOURCE.index('nginx_fence_status')
        window = self.SOURCE[max(0, start - 300):start + 900]
        self.assertIn('except Exception', window)


class TheRouteIsIntactTests(unittest.TestCase):
    def test_metrics_still_points_at_the_handler(self):
        from fastapi.routing import APIRoute
        hits = [r for r in main.app.routes
                if isinstance(r, APIRoute) and getattr(r, 'path', '') == '/metrics']
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics')

    def test_the_detector_is_not_a_route(self):
        from fastapi.routing import APIRoute
        endpoints = {r.endpoint.__name__ for r in main.app.routes
                     if isinstance(r, APIRoute)}
        self.assertNotIn('nginx_fence_status', endpoints)


class ItReflectsTheRealProductionShapeTests(unittest.TestCase):
    """用生产真实形态的一次事故快照做一次回归。"""

    def test_the_exact_rules_that_broke_production_are_detected(self):
        content = (
            'server {\n'
            '    # zh-container-maintenance-v1\n'
            '    if ($uri ~ ^/(api|qoder|trae|wb|workbuddy|zpush)(/|$)) { return 503; }\n'
            '    server_name zhihui.kaogong.art;\n'
            '}\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'zhihui.kaogong.art').write_text(content, encoding='utf-8')
            with mock.patch.object(operational, '_NGINX_CONFIG_PATHS', (tmp,)):
                status = operational.nginx_fence_status()
        self.assertTrue(status['fenced'])

    def test_the_cleaned_version_is_not_detected(self):
        """我们修复后删掉的正是标记行与那条规则。"""
        content = (
            'server {\n'
            '    server_name zhihui.kaogong.art;\n'
            '}\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'zhihui.kaogong.art').write_text(content, encoding='utf-8')
            with mock.patch.object(operational, '_NGINX_CONFIG_PATHS', (tmp,)):
                status = operational.nginx_fence_status()
        self.assertFalse(status['fenced'])


if __name__ == '__main__':
    unittest.main()
