"""健康巡检必须覆盖「配置能否被当前版本接受」和「发布树有没有字节码污染」。

## 为什么加这两项

**配置校验**：2026-11-23 23:25 有一个 drop-in 先加了 `WB_TRAFFIC_TAP`，
而当时代码的 schema 还不认识它。服务在「启动 → 被拒 → 3 秒后重启」之间
循环了约 8 分钟（`StartLimitBurst=10` 才停）。

规范 §5.1 说 `healthcheck.sh` 要「一条命令给出全局结论」。原来这条结论里
**没有「配置会不会被拒」** —— 而这正是那次故障的唯一原因。

**字节码污染**：发布树的完整性校验拒绝任何 `__pycache__`，而检查工具自己
import 发布树代码时就会生成它。`deploy/verify-runtime-config.py` 顶部那句
`sys.dont_write_bytecode = True` 就是为此。巡检里加一项能看见历史残留。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 巡检调用 `verify-runtime-config.py` | 配置会被拒这件事又只能在重启时发现 |
| 配置被拒时报 FAIL 而不是 warn | 会被当成"待观察"，而它实际上会让服务起不来 |
| 巡检会数发布树里的 `__pycache__` | 下一次 seal 失败时又要重新查一遍 |
| 巡检脚本仍然只读 | 检查工具绝不能改变被检查对象 |
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[2]/'deploy'/'healthcheck.sh').read_text(
    encoding='utf-8')


class ConfigSectionTests(unittest.TestCase):
    def test_calls_the_preflight_checker(self):
        self.assertIn('verify-runtime-config.py', SOURCE,
                      '巡检没有调用配置校验——那 8 分钟崩溃重启的成因就又看不见了')

    def test_config_rejection_is_fail_not_warn(self):
        """配置会被拒是「服务起不来」，不是「值得关注」。"""
        block = SOURCE.split("section '配置与发布树'", 1)
        self.assertEqual(len(block), 2, '「配置与发布树」这一节不见了')
        section = block[1].split('section ', 1)[0]
        self.assertIn('broken', section,
                      '配置被拒必须报 FAIL；报 warn 会被当成"待观察"')

    def test_counts_bytecode_leftovers(self):
        self.assertIn('__pycache__', SOURCE,
                      '巡检看不到发布树里的字节码残留')


class ReadOnlyTests(unittest.TestCase):
    def test_does_not_delete_or_modify_release_files(self):
        """巡检只读：只允许删它自己建的临时文件。"""
        dangerous = [line.strip() for line in SOURCE.splitlines()
                     if re.search(r'\brm\s+-[rf]', line)
                     and 'hc-' not in line]
        self.assertEqual(dangerous, [],
                         '巡检里出现了删文件的动作——它是只读检查工具')


class ThresholdSourceTests(unittest.TestCase):
    """判警告用的阈值必须来自线上，不能是代码里的魔数。

    ## 为什么

    2026-11-25 发现巡检每天报「3 次流式请求首字节超过 60 秒」，而线上实际
    `WB_FIRST_BYTE_TIMEOUT_S=240`。

    那个 60 是个**没有出处的魔数**：既不等于线上超时，也不等于任何配置值。
    在 240 秒预算下，60~240 秒之间给出首字节是**正常**的 —— 但每天都被算成
    一条警告。运维看到它会以为接近故障，实际还有 3 分钟余量。

    `server/config.py` 里也有一个 60 的默认值，同一个数字在三个地方含义不同
    （见 docs/OPS-PITFALLS.md 第十八条「代码里的默认值不是线上值」）。

    ## 本测试守什么

    | 不变量 | 破坏后的表现 |
    | --- | --- |
    | 阈值从 `systemctl show` 读 | 又变回魔数，警告失去语义 |
    | 读不到时有非数字兜底 | 读到异常值会让算术展开报错，巡检崩掉 |
    | 首字节比较不再硬编码 60000 | 240 秒预算下的正常请求继续被报成警告 |
    """

    def test_reads_threshold_from_running_unit(self):
        self.assertIn('systemctl show', SOURCE,
                      '阈值必须来自运行中的服务，不是代码里的常量')

    def test_first_byte_budget_is_not_hardcoded(self):
        self.assertNotIn('v > 60000', SOURCE,
                         '首字节阈值又硬编码成 60 秒了——线上预算是 240')
        self.assertIn('unit_env workbuddy-web WB_FIRST_BYTE_TIMEOUT_S', SOURCE,
                      '首字节预算没有从 WB_FIRST_BYTE_TIMEOUT_S 读')

    def test_unit_env_has_a_non_numeric_fallback(self):
        """读到空值或非数字时必须退回默认值，否则 `$((x * 1000))` 会报错。

        巡检是「一条命令给出全局结论」的那个命令，它自己不能崩。
        """
        self.assertIn('unit_env()', SOURCE, '缺少 unit_env 定义')
        block = SOURCE.split('unit_env()', 1)[1].split('}\n', 1)[0]
        self.assertIn('[!0-9]', block,
                      'unit_env 没有非数字兜底：读到异常值会让算术展开失败')
        self.assertIn('"$3"', block,
                      'unit_env 没有用调用方给的默认值')


class VacuumAdviceTests(unittest.TestCase):
    """巡检不能建议一个规范明令禁止的操作。

    ## 为什么

    原来判的是**绝对页数**：

    ```bash
    if [ "$free" -gt 200 ]; then
        note "空闲页 $free 偏多，可考虑 VACUUM 回收空间"
    ```

    线上库是 **185,588 页**。200 页 = **0.108%**。

    也就是说：只要攒下 800 KB 的空闲页，巡检就会建议对一个 725 MB 的库做
    VACUUM。

    而规范第六节写着：

    > 对 700 MB 级数据库无脑 VACUUM —— 先看空闲页；空闲页少说明没有碎片，
    > VACUUM 只锁表不解决任何问题

    **0.108% 显然属于「空闲页少」。巡检原来在建议一件被禁止的事。**

    ## 本测试守什么

    | 不变量 | 破坏后的表现 |
    | --- | --- |
    | 判比例而不是绝对页数 | 760 MB 的库和 7 MB 的库用同一个门槛 |
    | 阈值不低于 10% | 又会建议对碎片很少的库做全表锁 |
    | 文案写明「全程锁表」 | 只说「可考虑」，把决策成本藏起来 |
    | 取不到页数时不崩 | `$((free * 1000 / pages))` 在 pages 为空时直接报错 |
    """

    def test_does_not_use_absolute_page_count(self):
        self.assertNotIn('-gt 200', SOURCE,
                         'VACUUM 建议又变成绝对页数了——它对不同大小的库意义完全不同')

    def test_judges_by_ratio(self):
        self.assertIn('permille', SOURCE, '没有按比例判断')
        block = SOURCE.split('section \'数据库\'', 1)[1].split('section ', 1)[0]
        self.assertIn('pages', block, '比例计算要用到 page_count')

    def test_threshold_is_at_least_ten_percent(self):
        """10% 以下都不值得为一个 700 MB 的库全程锁表。"""
        self.assertIn('-ge 100', SOURCE,
                      '千分比阈值低于 100（10%）：又会建议对碎片很少的库做 VACUUM')

    def test_mentions_the_lock_cost(self):
        block = SOURCE.split('section \'数据库\'', 1)[1].split('section ', 1)[0]
        self.assertIn('锁表', block,
                      '没有说明 VACUUM 会锁表——规范禁止它的原因正是这个')

    def test_guards_against_missing_page_count(self):
        block = SOURCE.split('section \'数据库\'', 1)[1].split('section ', 1)[0]
        self.assertIn('"$pages" -gt 0', block,
                      'page_count 为 0 或空时会让算术展开报错，巡检会崩')


class RecoverySnapshotTests(unittest.TestCase):
    """巡检必须看得见「每日完整快照失败了」。

    ## 为什么

    2026-11-26：`zhihui-recovery.service` 连续两天失败（11-24、11-26），而巡检
    报的是「ok 28 项，警告 1 项，问题 0 项」—— 那 1 条警告还是上游账号数，与
    备份毫无关系。

    失败原因在发布树里：`server/backup_application.py` 的首行带 CRLF，内核直接
    执行时找不到 `python\\r` 这个解释器，错误却报成「文件不存在」。

    一个每天跑、失败了两天、而全局巡检仍然全绿的任务，等于没有监控。

    ## 本测试守什么

    | 不变量 | 破坏后的表现 |
    | --- | --- |
    | 巡检里有恢复快照这一节 | 备份停摆又只能靠人想起来去看 journal |
    | 快照过期判 FAIL | 报 warn 会被当成「待观察」，而它意味着恢复能力归零 |
    | 同时看 systemd 的失败状态 | 只比目录年龄会漏掉「今天失败、昨天那份还在」 |
    | 年龄阈值有上限 | 阈值太松时，失效一整天仍然显示 ok |
    """

    def _block(self) -> str:
        parts = SOURCE.split("section '恢复快照'", 1)
        self.assertEqual(len(parts), 2, '「恢复快照」这一节不见了')
        return parts[1].split('section ', 1)[0]

    def test_has_a_recovery_snapshot_section(self):
        self.assertIn("section '恢复快照'", SOURCE,
                      '巡检没有恢复快照这一节：备份停摆会完全静默')

    def test_checks_service_failure_state(self):
        block = self._block()
        self.assertIn('zhihui-recovery.service', block,
                      '没有检查 systemd 的失败状态：只比目录年龄会漏掉「今天失败」')
        self.assertIn('failed', block, 'service 处于 failed 时没有单独判据')

    def test_stale_snapshot_is_fatal_not_warning(self):
        block = self._block()
        self.assertIn('broken', block,
                      '快照过期必须判 FAIL：报 warn 会被当成"待观察"')
        self.assertNotIn('note "最近快照', block,
                         '快照过期不能降级成警告')

    def test_snapshot_age_threshold_is_bounded(self):
        self.assertIn('-le 36', self._block(),
                      '快照年龄阈值变了：超过 36 小时（每日一次加半天余量）就该 FAIL')

    def test_inactive_oneshot_is_judged_by_result(self):
        """oneshot 跑完就是 inactive：不能把它写成「没在运行」。"""
        block = self._block()
        self.assertIn('上次运行成功', block,
                      'oneshot 结束后只打印 inactive，会让人以为备份没在跑')
        self.assertIn('Result', block,
                      '没有读 systemd 的 Result，无法区分「跑完了」与「失败了」')

    def test_reads_the_snapshot_directory(self):
        self.assertIn('/root/backups/zhihui-v2/snapshots', self._block(),
                      '快照目录写错或缺失时，这一节会永远显示 ok')


class CreditTrendTests(unittest.TestCase):
    """额度行必须带上「在掉还是在涨」。

    2026-11-26 复核竞品报告时发现：巡检只报百分比，于是「剩 24% 但在涨」和
    「剩 24% 且在掉」看起来一样急。当时的真实数据里两条额度都在涨（trae 靠每日
    签到、workbuddy 池靠扩容），而 `workbuddy_low_accounts` 已经连续响了 19 小时。
    """

    def test_compares_against_a_day_old_snapshot(self):
        self.assertIn('captured_at <= $latest - 86400', SOURCE)

    def test_says_how_long_it_lasts(self):
        self.assertIn('按这速度', SOURCE)
        self.assertIn('天用完', SOURCE)

    def test_says_when_it_is_not_dropping(self):
        # 「在涨」和「没变」都要能说，否则读的人还是分不清。
        self.assertIn('近 24 小时增加了', SOURCE)
        self.assertIn('近 24 小时没有变化', SOURCE)

    def test_survives_a_missing_older_snapshot(self):
        # 刚上线时没有 24 小时前的快照：这时不能编一个趋势出来。
        self.assertIn('-1', SOURCE)
        self.assertIn('*[!0-9.]*) ;;', SOURCE)

    def test_does_not_change_the_verdict(self):
        # 趋势只是附加信息；分档仍按百分比，否则历史告警会集体变脸。
        self.assertIn('if [ "$pct" -lt 10 ]', SOURCE)
        self.assertIn('elif [ "$pct" -lt 20 ]', SOURCE)


class ErrorRateDenominatorTests(unittest.TestCase):
    """错误率的分母不能把健康探针算进去。

    2026-11-26 实测：24 小时 12,216 条请求记录里，`/api/readyz` 8,375、
    `/api/healthz` 1,812、`/api/release` 892 —— 探针占七成以上。拿总行数当分母，
    错误率被稀释三成，读起来比实际好，这正是规范里「不许把没机会出错当没出错」。
    """

    def test_counts_inference_requests_separately(self):
        self.assertIn("endpoint LIKE '/v1/%'", SOURCE)

    def test_prints_both_numbers(self):
        # 只说「推理 N 条」会藏掉总量；两个都给才看得出来探针占了多少。
        self.assertIn('其中推理', SOURCE)

    def test_falls_back_when_there_is_no_inference_traffic(self):
        # 刚上线或流量全来自控制台时，inference 为 0，不能除零。
        self.assertIn('base=$total', SOURCE)

    def test_verdict_still_uses_the_total_error_count(self):
        # 判断仍按总 5xx：控制台端点挂了同样是故障，不能因为不算推理就忽略。
        self.assertIn('[ "${errors:-0}" -eq 0 ]', SOURCE)


class ApplicationBackupVisibilityTests(unittest.TestCase):
    """巡检要看得见每日应用备份。

    机器上有两套备份：04:20 的加密快照（`zhihui-v2/snapshots`，巡检早就在看）与
    05:30 的应用备份（`workbuddy-daily/*.tar.gz`，把主库、门户库、配对账本打包，
    生成时真解出来校验过一次）。

    后者在 2026-11-23 与 11-25 各失败过一次 —— 首行 CRLF 让内核找不到解释器、
    OOM 撞上 512MB 限制 —— 而巡检当时报「问题 0」，失败信息只躺在
    /var/log/wb2api-backup.log 里。
    """

    def test_reads_the_latest_manifest(self):
        self.assertIn('/root/backups/workbuddy-daily/latest.json', SOURCE)

    def test_surfaces_the_restore_check_result(self):
        # latest.json 里 `restore_check` 是「生成时真的解出来校验过」的结论，
        # 比「文件在」强得多，要显示出来。
        self.assertIn('restore_check', SOURCE)

    def test_two_thresholds_not_one(self):
        # 每天一次：26 小时记待观察，50 小时（连续两次没成功）才判问题。
        self.assertIn('app_age_h" -gt 50', SOURCE)
        self.assertIn('app_age_h" -gt 26', SOURCE)

    def test_non_root_does_not_cry_wolf(self):
        # /root 下的目录非 root 读不到。读不到就报「有问题」是误报 ——
        # 这台机器上巡检常年用 sudo 跑，但别的调用方不一定。
        self.assertIn('id -u', SOURCE)

    def test_snapshot_and_application_backup_are_both_checked(self):
        # 两套互不替代，缺一个都算没覆盖。
        self.assertIn('/root/backups/zhihui-v2/snapshots', SOURCE)
        self.assertIn('workbuddy-daily', SOURCE)


class RuntimeAlertDetailTests(unittest.TestCase):
    """运行告警要说出「为什么」，而不只是「哪一项」。

    `/api/health/alerts` 刻意不返回 detail（里面可能有账号名与上游错误原文）。
    但巡检是宿主机上的 root，库就在本地 —— 从 `operational_alerts` 直接取，
    比「按名字去日志里翻」实际得多：`transition()` 进入时只写一行，持续 active
    的告警在日志里早就滚没了。

    2026-11-26 之前的输出只有「1 项预警：workbuddy_low_accounts」，
    看到它仍然不知道该不该处理。
    """

    def test_reads_detail_from_the_local_table(self):
        self.assertIn('FROM operational_alerts', SOURCE)
        self.assertIn('detail', SOURCE)

    def test_falls_back_when_the_table_is_unreadable(self):
        # sqlite3 缺失或库锁住时不能整段消失。
        self.assertIn('取不到明细', SOURCE)

    def test_keeps_the_endpoint_contract(self):
        # 端点仍然只给名字 —— 扩大它反而把内部术语暴露给回环上的其他进程。
        self.assertIn('/api/health/alerts', SOURCE)
        self.assertNotIn('"detail"', SOURCE.split('/api/health/alerts')[1][:400])


class AlertCountParserTests(unittest.TestCase):
    """巡检必须取**顶层** `count`（active 告警数），不能取 `notify.dropped.count`。

    DAN-41 在 `/api/health/alerts` 的响应里加了 `notify` 字段（`dropped.count`
    是进程内累计丢弃数）。原先的解析是
    `sed -n 's/.*"count":.../\\1/p'`，`.*` 贪婪，会命中**最后一个** `"count":`，
    于是巡检读到的成了丢弃数：

    - 重启后 `operational._load_alerts()` 把 ongoing 告警从库里恢复，丢弃计数
      归零 —— 有告警挂着，巡检却报「应用内告警全部为空」；
    - 反过来，历史丢弃数不为零时，告警已经恢复也会一直报预警。

    这里直接跑脚本里那一段 `acount=$(...)`，用真实响应形状验证取值。
    """

    ACOUNT = re.compile(r"^\s*(acount=\$\(.*\))\s*$", re.M)

    # 端点真实形状（server/main.py:health_alerts）+ 几种边界。
    SHAPES = {
        'active 3，丢弃 0（重启后计数归零）': (
            '{"service":"zhihui-gateway","count":3,'
            '"active":[{"name":"backup_evidence","age_seconds":10}],'
            '"notify":{"configured":false,"mode":"disabled",'
            '"dropped":{"count":0,"names":{},"last_at":0}}}', '3'),
        'active 0，历史丢弃 7（已恢复）': (
            '{"service":"zhihui-gateway","count":0,"active":[],'
            '"notify":{"configured":true,"mode":"file",'
            '"dropped":{"count":7,"names":{"backup_evidence":7},"last_at":1}}}', '0'),
        'active 12，丢弃 12（同值，靠结构而非数值区分）': (
            '{"service":"zhihui-gateway","count":12,"active":[],'
            '"notify":{"configured":false,"dropped":{"count":12}}}', '12'),
        'count 与下一个键之间有空格': (
            '{"count": 5 , "active":[],"notify":{"dropped":{"count":9}}}', '5'),
        '非告警响应（loopback 拒绝）': ('{"error":"loopback only"}', ''),
    }

    def _snippet(self) -> str:
        m = self.ACOUNT.search(SOURCE)
        self.assertIsNotNone(m, '脚本里找不到 acount= 的解析行')
        return m.group(1)

    def _run(self, payload: str) -> str:
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('bash 不可用，无法执行巡检脚本里的解析片段')
        script = 'alerts_json=$(cat)\n' + self._snippet() + '\nprintf %s "$acount"\n'
        p = subprocess.run([bash, '-c', script], input=payload,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_does_not_use_the_greedy_sed_parser(self):
        """贪婪 sed 会取到最后一个 count —— 这正是 DAN-41 复评指出的回归。

        只看 `acount=` 那一行本身：上面的注释里为了讲清楚问题会引用旧写法，
        不能因为注释提到 sed 就判红。
        """
        line = self._snippet()
        self.assertNotIn('sed', line,
                         'acount 仍用 sed 解析：`.*` 贪婪会命中 notify.dropped.count')
        self.assertIn('awk', line)

    def test_reads_the_top_level_count_for_every_shape(self):
        for label, (payload, want) in self.SHAPES.items():
            with self.subTest(shape=label):
                self.assertEqual(self._run(payload), want,
                                 f'{label}: 巡检取错了 count 字段')


if __name__ == '__main__':
    unittest.main()
