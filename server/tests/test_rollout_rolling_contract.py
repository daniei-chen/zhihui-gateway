"""`deploy/rollout-rolling.sh` 的契约测试。

## 为什么要专门守这个脚本

规范第三节的全部承诺都压在这一个脚本上：**发版不得打断在途调用**。
它做对了，用户感觉不到发布；它做错了，每一次发版都会丢掉正在生成的回答。

而这个脚本此前**没有任何测试**：`test_v12152_rollout_script.py` 测的是
另一个脚本 `deploy/rollout.sh`（prepare/run/cleanup 三段式），两者名字像、
职责完全不同。

## 2026-11-25 发现的两个真问题

**其一，版本号前缀没归一化。** 发 v1.5.92 时它报：

```
!! 重启后 60 秒仍未返回 vv1.5.92，请检查 workbuddy-web
```

双 v。服务其实已经正常返回 `{"version":"1.5.92",...}`，五个页面全 200。
原因是 `/api/release` 里 `version` 字段不带 v、`release_id` 字段带 v，
而脚本把调用方传进来的 `v1.5.92` 直接拼进 `"version":"$version"` 去 grep。

**其二，5xx 统计没有限定推理流量。** 判据是：

```bash
awk -v s="$before" 'NR>s && $9 ~ /^5/' "$ACCESS_LOG"
```

`/var/log/nginx/access.log` 是整台机器的**共享日志**，同一个文件里还有
控制台页面、`/_next` 静态资源、`/dsh-login`（属于另一个站点）。
2026-11-25 实测：这份日志的 5xx 里 8 条属于 `/v1/`，另有 4 条不是 ——
那 4 条会被算进「本次发版丢了几个请求」。

## 为什么这两个都算「值得修的缺陷」

后果都不是「发布失败」，而是更麻烦的东西：**发布成功了却看起来像失败**。
脚本 `die` 之后连 5xx 统计都没跑，退出码也没给 —— 而那个统计正是
「本次发版有没有丢请求」的唯一证据。

**一个会误报的判据比没有判据更糟**，因为下次真出问题时它不再被相信。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 版本号先归一化再比对 | 传 `v1.5.92` 时永远匹配不上，成功发布被判失败 |
| 5xx 只统计 `/v1/` | 无关站点的 5xx 被算成本次发布丢的请求 |
| 计数与明细都过滤 | 只改一处，明细里仍会出现无关请求 |
| `--check` 不产生副作用 | 体检变成真发布 |
| 中继先于网关重启 | 顺序反了，中继重启那 1-2 秒丢请求 |
| 退出码 0/1/2 分明 | 「有 5xx」和「前置条件不满足」被混成一体 |
| 发布指针必须先到位 | 对着旧指针重启，等于没发布 |

这些都是静态断言（读源码比对），不执行脚本 —— 它要 root、要动 systemd，
不适合在单测里跑。守的是**结构**：该有的语句还在不在。
"""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'rollout-rolling.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')


class VersionArgumentIsNormalizedTests(unittest.TestCase):
    """2026-11-25 的 `vv1.5.92` 误报。"""

    def test_it_strips_a_leading_v(self):
        self.assertIn(
            'version="${version#v}"', SOURCE,
            '没有归一化版本号前缀：传 v1.5.92 时 "version":"$version" 会变成 '
            '"version":"v1.5.92"，而接口返回的是 "version":"1.5.92"，永远匹配不上')

    def test_the_strip_happens_before_the_grep(self):
        strip_at = SOURCE.index('${version#v}')
        grep_at = SOURCE.index('"\\"version\\":\\"$version\\""')
        self.assertLess(
            strip_at, grep_at,
            '归一化必须在比对之前，否则改了个寂寞')

    def test_the_failure_message_still_prints_one_v(self):
        # 消息里是 "v$version"；归一化之后正好是 v1.5.92，不能变成 vv1.5.92。
        self.assertIn('v$version', SOURCE)


class CheckModeIsReadOnlyTests(unittest.TestCase):
    def test_check_returns_before_any_mutation(self):
        check_at = SOURCE.index('--check')
        restart_at = SOURCE.index('systemctl restart')
        self.assertLess(
            check_at, restart_at,
            '--check 必须在任何改动之前返回')

    def test_it_says_it_did_nothing(self):
        self.assertIn('体检通过，未做任何改动', SOURCE)


class RelayRestartsBeforeGatewayTests(unittest.TestCase):
    """规范 3.2：顺序颠倒就白做。"""

    def test_relay_restart_precedes_gateway_restart(self):
        # 看 main() 里的**调用点**，不是函数定义的位置 —— 定义顺序与执行顺序无关。
        relay = SOURCE.index('        restart_relay')
        gateway = SOURCE.index('    restart_gateway "$version"')
        self.assertLess(
            relay, gateway,
            '中继必须先于网关重启 —— 反代已接中继时，先重启中继会丢 1-2 秒请求')

    def test_it_warns_when_relay_changed_is_omitted(self):
        self.assertIn(
            '--relay-changed', SOURCE,
            '必须提示调用方：中继代码改了却不加参数，中继会继续跑旧代码')


class FiveHundredsOnlyCountInferenceTests(unittest.TestCase):
    """共享 access.log 里还有别的站点，必须只统计推理流量。"""

    def test_the_awk_filter_is_scoped_to_v1(self):
        self.assertIn(
            r'$7 ~ /^\/v1\//', SOURCE,
            '5xx 统计没有限定 /v1/：共享日志里其他站点的 5xx '
            '会被算成本次发布丢的请求')

    def test_both_the_count_and_the_listing_are_scoped(self):
        self.assertEqual(
            SOURCE.count(r'$7 ~ /^\/v1\//'), 2,
            '计数和明细是两处 awk，都要过滤，漏一处就前功尽弃')

    def test_it_reports_the_window_request_count(self):
        self.assertIn(
            '重启窗口内的 5xx', SOURCE,
            '没有窗口请求数，「零中断」就是一句无法核对的话')


class ExitCodesAreDistinctTests(unittest.TestCase):
    """规范 3.3 的三种结局必须能区分。"""

    def test_gateway_failure_and_five_hundreds_differ(self):
        # 前置条件不满足（die → exit 1）与「已上线但有 5xx」必须是不同退出码，
        # 否则调用方无法判断该修环境还是该排查流量。
        self.assertIn(
            'return 2', SOURCE,
            '缺少「已部署但有 5xx」的专用退出码（2）')

    def test_main_propagates_the_status(self):
        self.assertIn(
            'return "$status"', SOURCE,
            'main 必须把 restart_gateway 的状态码透出去，否则脚本永远退出 0')

    def test_it_declares_zero_as_success(self):
        self.assertIn('零中断', SOURCE)


class PointerMustMatchFirstTests(unittest.TestCase):
    def test_it_refuses_when_pointer_differs(self):
        self.assertIn('release.py activate', SOURCE,
                      '必须提示先 activate —— 对着旧指针重启等于没发布')

    def test_it_verifies_the_pointer(self):
        self.assertIn('readlink -f', SOURCE)


class GatewayMustActuallyComeBackTests(unittest.TestCase):
    def test_it_polls_the_release_endpoint(self):
        self.assertIn('/api/release', SOURCE)

    def test_the_wait_is_bounded(self):
        self.assertIn('seq 1', SOURCE)


if __name__ == '__main__':
    unittest.main()
