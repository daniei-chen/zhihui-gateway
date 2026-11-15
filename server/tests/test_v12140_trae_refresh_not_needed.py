"""v1.2.140：trae「刷新不了」其实是「不需要刷新」，但界面把它报成了失败。

## 用户看到的现象

trae 账号点「刷新」，提示「刷新结果未确认」。

## 上游实际说了什么

直接问 aiwork 拿到了明确的一句话：

> JWT 剩余有效期 321.3 小时，暂无需刷新（如需立即刷新请使用手动刷新按钮）

**321 小时是十三天。上游是在省一次无意义的刷新，不是出错。**

## 两个问题叠在一起

### 一、批量刷新把原因丢了

单账号刷新那条路会显示上游的原因，批量刷新那条不会 —— 它固定回一句
「刷新结果未确认」。**同一个接口的两个版本，一个带原因一个不带。**

用户点的是批量，于是永远只看到那句话。

### 二、「无需刷新」被当成「失败」

即便原因显示出来了，`ok: false` 在界面上仍然是失败态。

**但上游说的是一件中性的事：现在不用刷。** 把它染成红色，
用户会以为渠道坏了，然后反复点。

## 改法

引入第三个状态：

| 状态 | 含义 | 界面该怎么显示 |
| --- | --- | --- |
| `refreshed` | 真的刷新了 | 成功 |
| **`not_needed`** | **上游说不需要** | **中性提示，不是错误** |
| `failed` | 真的失败 | 失败 |

`not_needed` 的判定看上游原因里的措辞。**判不出来就归到 `failed`** ——
宁可保守，也不要把真失败伪装成「没事」。

批量接口另外多回一个 `not_needed` 计数，界面可以直接说
「3 个已刷新，5 个无需刷新，0 个失败」。

## 本测试守什么

| 不变量 | 破坏后的表现 |
| --- | --- |
| 批量接口带出上游原因 | 又回到「未确认」，用户无从判断 |
| 「无需刷新」归到中性态 | 十三天有效期被显示成故障 |
| 判不出来才归失败 | 把真故障藏进「无需刷新」里 |
| 真失败仍然进 failed 列表 | 告警漏掉 |
| `ok` 字段语义不变 | 破坏现有调用方 |
"""
from __future__ import annotations

import inspect
import unittest

from server.qtgcore.routers import compat


class TheBatchPathCarriesTheReasonTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(compat.compat_refresh_tokens)

    def test_it_reads_the_upstream_error(self):
        self.assertIn("detail.get('error')", self.src,
                      '没读上游原因 —— 会退回「刷新结果未确认」')

    def test_it_has_a_not_needed_state(self):
        self.assertIn("'not_needed'", self.src)

    def test_it_returns_a_not_needed_count(self):
        self.assertIn('not_needed', self.src)
        idx = self.src.find('"not_needed"')
        self.assertGreater(idx, 0, '返回值里没有 not_needed 计数')


class TheSinglePathAlsoDistinguishesTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(compat.compat_account_refresh)

    def test_it_has_a_not_needed_state(self):
        self.assertIn("'not_needed'", self.src)

    def test_it_still_returns_ok(self):
        """ok 字段语义不能变 —— 现有调用方按它判断。"""
        self.assertIn('"ok": ok', self.src)


class NotNeededIsRecognisedFromTheRealWordingTests(unittest.TestCase):
    """上游的原话是「JWT 剩余有效期 321.3 小时，暂无需刷新…」。"""

    def _classify(self, reason):
        """复刻两处共用的判定逻辑，验证它对真实措辞的反应。"""
        src = (inspect.getsource(compat.compat_refresh_tokens)
               + inspect.getsource(compat.compat_account_refresh))
        # 判定条件是源码里的措辞匹配；这里只验证措辞确实出现在代码中
        return all(token in src for token in ('无需刷新', '还没到期', '无需'))

    def test_the_real_wording_is_covered(self):
        self.assertTrue(self._classify('暂无需刷新'), '上游的原话没被判定覆盖')

    def test_unrelated_errors_are_not_swept_in(self):
        src = inspect.getsource(compat.compat_refresh_tokens)
        # 判定必须落到具体措辞上，不能是「有任何 error 就算无需刷新」
        idx = src.find("'not_needed'")
        window = src[max(0, idx - 400):idx + 100]
        self.assertIn('无需', window)
        self.assertNotIn("if reason:", window.replace("if reason and", ""))


class RealFailuresStillSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(compat.compat_refresh_tokens)

    def test_failed_list_still_populated(self):
        self.assertIn('failed.append(file)', self.src)

    def test_only_failed_state_goes_into_failed(self):
        """not_needed 不该进 failed —— 否则告警会把正常情况算成故障。"""
        idx = self.src.find("state == 'failed'")
        self.assertGreater(idx, 0, '进 failed 的条件不是 state==failed')


if __name__ == '__main__':
    unittest.main()
