"""v1.2.82：两处指标的口径在 help 里没写清，读的人会得出错误结论。

## 问题一：`workbuddy_requests_total` 包含健康探测

实测（2026-11-15，进程刚起 4 分钟）：

```
workbuddy_requests_total                                   27
workbuddy_request_outcomes_total{traffic="probe"}           4
workbuddy_request_outcomes_total{traffic="inference"}      22
```

**27 = 4 探针 + 22 业务 + 1 管理。** 把 27 当成业务量会高估。

分类其实早就有了（`traffic` 有 `inference` / `admin` / `probe` / `other` 四档），
只是 help 写的是「Tracked requests」，读起来像业务量。**指着它去判断需求，是误读。**

## 问题二：进程 RSS 与 cgroup 数值不能直接相减

同一时刻实测：

| 项 | 进程级 | cgroup 级 |
| --- | --- | --- |
| **anon** | **124.8 MiB** | **124.8 MiB** |
| file | 18.9 MiB | 12.2 MiB |
| 合计 | 143.8 MiB | 138.4 MiB |

**anon 完全一致，差的全在 file** —— 进程 RSS 计入共享库映射页，cgroup 的 file 只算
实际驻留的页缓存。

所以两个数差几 MiB 是**口径差异，不是泄漏**。此前 help 没说，容易让人去追一个
不存在的缺口。
"""
from __future__ import annotations

import inspect
import pathlib
import unittest

from server import main, runtimemetrics


class RequestsTotalHelpSaysWhatItCountsTests(unittest.TestCase):
    def test_help_mentions_probes(self):
        source = inspect.getsource(runtimemetrics.Metrics.render)
        start = source.index('workbuddy_requests_total')
        window = source[start:start + 320]
        self.assertIn('probe', window.lower(),
                      '不说明含探针，读者会把总数当业务量')

    def test_help_points_at_the_classified_metric(self):
        source = inspect.getsource(runtimemetrics.Metrics.render)
        start = source.index('workbuddy_requests_total')
        window = source[start:start + 320]
        self.assertIn('workbuddy_request_outcomes_total', window,
                      '要说清真实业务量该看哪个指标')


class TrafficClassificationExistsTests(unittest.TestCase):
    """分类是既有能力，这里只钉住它别被改掉。"""

    def test_four_traffic_classes(self):
        self.assertEqual(set(runtimemetrics.TRAFFIC),
                         {'inference', 'admin', 'probe', 'other'})

    def test_unknown_traffic_falls_back_to_other(self):
        metrics = runtimemetrics.Metrics()
        metrics.observe(protocol='http', traffic='nonsense', http_status=200,
                        terminal='completed', latency_ms=1.0)
        keys = [k for k in metrics.outcomes if k[1] == 'other']
        self.assertTrue(keys, '未知 traffic 必须归到 other，不能凭空造标签')


class ResidentHelpSaysItDiffersFromCgroupTests(unittest.TestCase):
    def test_help_warns_against_subtracting(self):
        source = inspect.getsource(main.prometheus_metrics)
        start = source.index('workbuddy_process_resident_bytes')
        window = source[start:start + 500]
        self.assertIn('must not be subtracted', window,
                      '两个口径不同，直接相减会得到不存在的差值')

    def test_help_explains_the_reason(self):
        source = inspect.getsource(main.prometheus_metrics)
        start = source.index('workbuddy_process_resident_bytes')
        window = source[start:start + 500]
        self.assertIn('shared library', window,
                      '要说出差在哪，否则读者只能猜')


class CgroupHelpSaysTheSameTests(unittest.TestCase):
    def test_peak_help_mentions_the_opposite_direction(self):
        source = inspect.getsource(main.prometheus_metrics)
        start = source.index('workbuddy_cgroup_memory_peak_bytes')
        window = source[start:start + 500]
        self.assertIn('lower', window,
                      'cgroup 侧读出来会略低，这一点要从它自己那一侧也说明')


class TheMetricsRouteStillWorksTests(unittest.TestCase):
    def test_route_points_at_the_handler(self):
        from fastapi.routing import APIRoute
        hits = [r for r in main.app.routes
                if isinstance(r, APIRoute) and getattr(r, 'path', '') == '/metrics']
        self.assertEqual(hits[0].endpoint.__name__, 'prometheus_metrics')

    def test_render_produces_valid_prometheus_lines(self):
        metrics = runtimemetrics.Metrics()
        metrics.observe(protocol='openai', traffic='inference', http_status=200,
                        terminal='completed', latency_ms=12.0)
        lines = metrics.render()
        self.assertTrue(any(line.startswith('# HELP workbuddy_requests_total')
                            for line in lines))
        self.assertTrue(any(line.startswith('workbuddy_requests_total ')
                            for line in lines))


class TheDocsStayHonestTests(unittest.TestCase):
    """这两条口径差异要能被后来的人找到，而不只是藏在 help 里。"""

    def test_capabilities_doc_exists(self):
        doc = (pathlib.Path(__file__).resolve().parents[2]
               / 'docs' / 'UPSTREAM-CAPABILITIES.md')
        self.assertTrue(doc.is_file())


if __name__ == '__main__':
    unittest.main()
