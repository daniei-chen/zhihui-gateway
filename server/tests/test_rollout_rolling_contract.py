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

**其三，只按 `/v1/` 过滤仍然不够（2026-11-26）。** 另一个站点
`zhijing.kaogong.art` 也代理 `/v1/models`，它上游那台机器当时连不上，7 条 502
的时间正好落在 v1.6.1 的重启窗口里 —— 于是「零中断」被判成失败，而真正进入
网关的 8 条请求全部 200。路径前缀不是站点标识，判据改成读网关自己的请求终态。

## 为什么这两个都算「值得修的缺陷」

后果都不是「发布失败」，而是更麻烦的东西：**发布成功了却看起来像失败**。
脚本 `die` 之后连 5xx 统计都没跑，退出码也没给 —— 而那个统计正是
「本次发版有没有丢请求」的唯一证据。

**一个会误报的判据比没有判据更糟**，因为下次真出问题时它不再被相信。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 版本号先归一化再比对 | 传 `v1.5.92` 时永远匹配不上，成功发布被判失败 |
| 5xx 只统计网关自己的请求终态 | 别的站点同名路径的 5xx 被算成本次发布丢的请求 |
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
HEALTHCHECK = SCRIPT.parent / 'healthcheck.sh'
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

    def test_the_success_message_also_normalizes(self):
        """`main()` 里的完成消息同样要去前缀。

        2026-11-25 第一次修的时候只改了 `restart_gateway` 内部的局部变量，
        判断路径对了（不再 die），但 `main()` 结尾打印的还是
        `vv1.5.93 已上线` —— 结论正确、显示错误，一样会让人怀疑发布到底成没成。
        """
        self.assertIn(
            '"${version#v}"', SOURCE,
            '完成消息没有归一化：会打印出 vv1.5.93')


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


class FiveHundredsComeFromGatewayTerminalStatesTests(unittest.TestCase):
    """判据必须来自网关自己的请求终态，而不是整机共享的 nginx 日志。

    ## 2026-11-26 的真实误报

    v1.6.1 发布时脚本退出 2，报「窗口内 7 条 502」。查下来 7 条**全部**属于
    另一个站点 `zhijing.kaogong.art`：它把 `/v1/models` 反代到境外上游，而那台
    机器当时连不上（`upstream prematurely closed connection`）。时间恰好落在
    本次重启窗口里，被 `/v1/` 前缀过滤一起算了进来。

    同一窗口里真正进入网关的 8 条请求全部 200 —— 发布本身是干净的。

    `/v1/` 这个前缀两个站点都有，路径过滤天生不够。请求终态表只收网关处理过的
    请求，是唯一能同时满足「覆盖真实流量」和「排除别的站点」的口径。

    ## 本测试守什么

    | 不变量 | 破坏后的表现 |
    | --- | --- |
    | 起点取自 request_outcomes 的 rowid | 回到共享日志，别的站点再次污染判据 |
    | 起点在重启**之前**取 | 重启期间的请求被漏掉，永远数不到 5xx |
    | 计数与明细都按 rowid 过滤 | 只改一处，明细里仍会出现无关请求 |
    | 读不到终态表时 die | 没有判据却退出 0，等于宣称了一个没验证过的零中断 |
    """

    def test_counts_from_request_outcomes(self):
        self.assertIn('request_outcomes', SOURCE,
                      '没有读请求终态表：判据又回到共享日志了')
        self.assertNotIn(r'$7 ~ /^\/v1\//', SOURCE,
                         '还在按 /v1/ 前缀过滤共享 access.log：'
                         '另一个站点同名路径的 5xx 会被算成本次发布丢的请求')

    def test_the_starting_point_is_taken_before_the_restart(self):
        start = SOURCE.index('max(rowid)')
        restart = SOURCE.index('systemctl restart "$GATEWAY"')
        self.assertLess(start, restart, '起点必须在重启之前取，否则窗口为空')

    def test_count_and_detail_are_both_scoped(self):
        self.assertGreaterEqual(
            SOURCE.count('rowid > $before'), 2,
            '计数和明细是两处查询，都要过滤，漏一处就前功尽弃')

    def test_missing_terminal_table_is_fatal(self):
        self.assertIn('无法证明本次发版零中断', SOURCE,
                      '读不到终态表时必须 die：没有判据不能宣称零中断')

    def test_it_reports_the_window_request_count(self):
        self.assertIn('请求 %s 条，5xx %s 条', SOURCE,
                      '没有窗口请求数，「零中断」就是一句无法核对的话')

    def test_zero_requests_is_called_out(self):
        self.assertIn('不能据此宣称零中断', SOURCE,
                      '窗口内 0 条请求时不能算成功 —— 那只是没人在用')


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


class CandidateInstanceRunsBeforeTheRestartTests(unittest.TestCase):
    """发布前先起候选实例：拿几秒 CPU 换「线上不会因为起不来而断」。

    2026-11-23 真实踩过：drop-in 加了一个新版 schema 不认识的变量，重启后每 3 秒
    崩一次、直到 StartLimitBurst 才停，约 8 分钟完全不可用。配置校验能挡那一类，
    但挡不住 import 失败、迁移失败、静态资源缺失 —— 那三样只有真启动才知道。

    仓库里早就有候选实例的 unit（7865、`WB_BACKGROUND_JOBS=0`、共享入账），
    但发布脚本从来没用过：每次都拿线上那 6 秒空窗去赌「这一版能起来」。
    实测起一次约 6 秒，失败时线上一个请求都没被动过。
    """

    def test_candidate_runs_before_the_gateway_restart(self):
        candidate = SOURCE.index('    prestart_candidate "$version"')
        restart = SOURCE.index('    restart_gateway "$version"')
        self.assertLess(candidate, restart, '候选实例必须在重启之前')

    def test_it_uses_the_existing_helper_unit_and_port(self):
        self.assertIn('HELPER=workbuddy-web-helper', SOURCE)
        self.assertIn('CANDIDATE_PORT=7865', SOURCE,
                      '候选实例必须用 7865：那是 helper unit 里写死的端口')

    def test_failure_aborts_before_touching_production(self):
        self.assertIn('线上仍在跑旧代码', SOURCE,
                      '候选实例失败时要明说「线上没动过」，否则调用方会以为已经切了')

    def test_it_stops_the_candidate_on_every_path(self):
        # 就绪后停、没就绪也停、版本不符也停：三条路径都不能把 7865 留在监听状态。
        self.assertGreaterEqual(
            SOURCE.count('systemctl stop "$HELPER"'), 3,
            '有一条失败路径没有停候选实例，7865 会被留在监听状态')

    def test_check_mode_never_starts_it(self):
        check_return = SOURCE.index('体检通过，未做任何改动')
        # 带换行：蓝绿分支里那处调用是 `prestart_candidate "$version" 1`，不能算进来。
        candidate = SOURCE.index('    prestart_candidate "$version"\n')
        self.assertLess(check_return, candidate, '--check 不能起候选实例')

    def test_missing_unit_warns_instead_of_blocking(self):
        self.assertIn('跳过这一步', SOURCE,
                      'unit 不存在时应降级为提示，而不是让发布无法进行')


class BlueGreenHandsTheTrafficOverTests(unittest.TestCase):
    """蓝绿：让候选实例真的接流量，再重启主实例。

    `--blue-green` 与常态发布的区别只有一处：候选实例不再「检查完就停」，而是先把
    中继路由切到它，再停主实例、起主实例，最后切回来。重启那几秒里有另一个实例在
    服务，所以不依赖重试兜底。

    2026-11-26 实测过这条路的两端：把路由写成 `new` 后，经中继的请求出现在候选
    实例的日志里；切回 `old` 后出现在主实例日志里。中继在每次请求时重读路由文件，
    不需要重启它。
    """

    def test_blue_green_is_the_default(self):
        # 默认就走蓝绿；要退回重启必须显式声明。
        self.assertIn('blue_green=1 arg', SOURCE)
        self.assertIn('--no-blue-green) blue_green=0', SOURCE)
        self.assertIn('rollout_blue_green "$version"', SOURCE)
        # 退回路径仍在：没有候选实例的机器也能发版。
        self.assertIn('    prestart_candidate "$version"\n', SOURCE)

    def test_missing_candidate_falls_back_instead_of_failing(self):
        self.assertIn('这台机器上没法蓝绿发布，退回', SOURCE)
        self.assertIn('prestart_candidate "$version" 1 || return 3', SOURCE)
        self.assertIn('if [ "$status" -ne 3 ]', SOURCE)

    def test_route_moves_before_the_old_instance_stops(self):
        switch = SOURCE.index('    route_switch new')
        stop = SOURCE.index('    systemctl stop "$GATEWAY" || die')
        self.assertLess(switch, stop, '必须先把流量切走，再停旧实例')

    def test_route_comes_back_after_the_main_instance_is_ready(self):
        ready = SOURCE.index("    step '主实例已就绪，把流量切回来'")
        back = SOURCE.index('    route_switch old')
        self.assertLess(ready, back)
        # 切回之后才停候选实例，而且停之前必须过排空判据（2026-11-28 审计 D-04）。
        # 原来这里守的是「route_switch old → sleep 1 → systemctl stop」这个连续代码块，
        # 但那个 sleep 1 正是缺陷本身：它只证明等了一秒，不证明在途请求跑完了。
        # 现在的顺序与失败路径由 test_rollout_candidate_drain.py 逐条测。
        self.assertIn('    route_switch old\n', SOURCE)
        self.assertNotIn('    route_switch old\n    sleep 1\n    systemctl stop "$HELPER"',
                         SOURCE,
                         'sleep 1 之后直接停候选 = D-04：在途的流没跑完就被截断')

    def test_a_failed_restart_leaves_the_candidate_serving(self):
        self.assertIn('线上仍由候选实例服务', SOURCE,
                      '主实例起不来时必须说明服务没断，否则运维会直接停候选实例')
        self.assertIn('不要先停掉', SOURCE)

    def test_route_switch_writes_atomically_and_advances_epoch(self):
        self.assertIn('mv -f "$tmp" "$ROUTE_FILE"', SOURCE)
        self.assertIn('"epoch":%d', SOURCE)
        self.assertIn("sed -n 's/.*\"epoch\":\\([0-9]*\\).*/\\1/p'", SOURCE,
                      'epoch 必须从现有文件读出来再 +1，否则中继会当成过期路由')

    def test_pages_are_checked_the_same_way_in_both_paths(self):
        # 页面检查两边共用同一段，避免蓝绿模式漏掉这一环。
        self.assertEqual(SOURCE.count("    check_pages\n"), 2)


class RolloutResultIsRecordedTests(unittest.TestCase):
    """每次发布的结果要落盘，而且巡检要能看见。

    2026-11-26 09:45 那次发版走远程会话，命令 30 秒超时，脚本的 stdout 全丢；
    发布本身是成功的，但「窗口多少请求、多少 5xx、用的蓝绿还是重启」都没有留痕，
    只能事后翻 request_outcomes 反推。结论没留痕，等于没有结论。
    """

    def test_results_are_appended_not_overwritten(self):
        self.assertIn('ROLLOUT_LOG=', SOURCE)
        self.assertIn('>> "$ROLLOUT_LOG"', SOURCE, '必须追加；覆盖会把历史发布记录抹掉')

    def test_both_modes_record_the_window(self):
        # 蓝绿与重启两条路径都要写，否则从记录里看不出用的哪种方式。
        self.assertIn('record_rollout "$version" \'蓝绿\'', SOURCE)
        self.assertIn('record_rollout "$version" \'重启\'', SOURCE)

    def test_an_interrupted_rollout_still_leaves_a_line(self):
        self.assertIn('trap on_exit EXIT', SOURCE)
        self.assertIn('未走到统计环节', SOURCE)

    def test_check_mode_never_writes(self):
        # --check 不改任何状态，也不该往发布记录里写东西。
        self.assertIn('[ "$CHECK_ONLY" != 1 ]', SOURCE)

    def test_success_is_recorded_after_required_page_checks(self):
        # 记录窗口统计不能提前宣布发布成功；中断由 EXIT trap 记录未完成。
        for function, mode in (('rollout_blue_green', "'蓝绿'"),
                               ('restart_gateway', "'重启'")):
            start = SOURCE.index(function + '() {')
            pages = SOURCE.index('    check_pages\n', start)
            recorded = SOURCE.index(
                'record_rollout "$version" ' + mode + ' "$total" "$failures" 0', start)
            self.assertGreater(recorded, pages, '成功记录必须在必需检查完成之后')

    def test_healthcheck_shows_the_last_rollout(self):
        health = HEALTHCHECK.read_text(encoding='utf-8')
        self.assertIn("section '最近一次发布'", health)
        self.assertIn('rollout-log.jsonl', health)
        self.assertIn('不能据此宣称零中断', health)


class RelayRestartDoesNotDropTrafficTests(unittest.TestCase):
    """重启中继那 1-2 秒也不再是盲区。

    中继是单进程：停它、起它，中间没人接流量。以前的做法是「接受这 1-2 秒」。
    现在先把推理入口切到直连网关（reload 平滑，在途的流不受影响），中继起来后切回。

    2026-11-26 实测过两端：直连时请求出现在网关日志，切回后出现在中继日志。
    """

    def test_entry_moves_to_direct_before_the_restart(self):
        switch = SOURCE.index('    switch_inference direct')
        restart = SOURCE.index('    systemctl restart "$RELAY"')
        self.assertLess(switch, restart, '必须先把入口切走，再停中继')

    def test_entry_comes_back_after_the_relay_is_ready(self):
        ready = SOURCE.index('    printf \'   中继已就绪（pid %s）\\n\' "$pid"')
        back = SOURCE.rindex('    switch_inference relay')
        self.assertLess(ready, back)

    def test_a_dead_relay_keeps_the_entry_on_direct(self):
        # 中继起不来时把入口切回去 = 把流量指向一个死端口。留在直连，服务照常。
        self.assertIn('推理入口保持在直连', SOURCE)
        self.assertIn('推理入口停在直连', SOURCE)

    def test_the_switch_is_guarded_by_nginx_t(self):
        body = SOURCE[SOURCE.index('switch_inference() {'):SOURCE.index('restart_relay() {')]
        self.assertIn('nginx -t', body)
        self.assertIn('已还原', body)

    def test_healthcheck_reports_where_the_entry_points(self):
        health = HEALTHCHECK.read_text(encoding='utf-8')
        self.assertIn('推理入口指向中继', health)
        self.assertIn('推理入口直连网关', health)


if __name__ == '__main__':
    unittest.main()
