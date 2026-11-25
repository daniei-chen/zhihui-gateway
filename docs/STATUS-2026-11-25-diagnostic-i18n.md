# 第 65 轮：一轮纯诊断，以及两次测量错误

规范 §2.1 说：「不要在没有 P0/P1 的时候硬凑改动。找不到值得做的，就做诊断和验证，
把结论写清楚。」

这一轮就是这样的一轮。**没有代码改动**，但我犯了两次测量错误，第二次才纠正了第一次 ——
这部分值得写下来。

## 一、第一个候选：那条一直没修的硬编码

`app/(main)/security/page.tsx:602` 长期被记作「唯一剩下的硬编码中文」：

```tsx
{a.actor === '(空)' ? t('security.actorEmpty') : a.actor || '—'}
```

**它不是缺陷。**

`'(空)'` 是比较**后端返回的字面量**，展示走 `t()`。这正是规范 §2.2 要求的：

> 服务端返回的文案、上游的错误原文**不要翻译、不要改写**，
> 只在本地兜底字面量上做国际化。

改掉它反而会破坏「存储层语言中立」这个设计 —— 后端把「没填用户名」存成 `(空)`，
是因为同一条记录不能因写入者语言不同而不一致。

**结论：不改。这条从来就不是待办。**

## 二、第二个候选：i18n 键完整性 —— 第一次测量是错的

想法：如果某个文案键只在简体中文里有，英文用户会看到中文（回退到源语言），
而不是报错。这是个静默的用户可见缺陷。

于是写了个脚本，拍平五个语言文件的键集做差集。结果：

```
zh-CN leaf keys: 2339
zh-TW : missing=0 extra=250
en    : missing=9 extra=268     ← 英文缺 9 个
ja    : missing=0 extra=250
ko    : missing=0 extra=250
```

**看起来是个真缺陷，而且是讽刺性的**：英文是最常见的第二语言，却唯一残缺。

那 9 个全是「数字 + 量词」：

```
credit.expiresInDays     {n} 天后到期
credit.expiresInHours    {n} 小时后到期
credit.expiresInMinutes  {n} 分钟后到期
dashboard.abnormal       {n} 个异常
format.days              {n} 天
format.hours             {n} 小时
format.minutes           {n} 分钟
format.seconds           {n} 秒
metric.creditAmount      {n} 积分
```

**但它是假的。**

英文有单复数，这些键在 `en.json` 里是**复数对象**：

```json
"minutes": {"one": "{n} minute", "other": "{n} minutes"}
```

而简体中文是字符串 `"{n} 分钟"`。我的拍平函数把 `{one, other}` 当普通对象递归展开，
得到 `format.minutes.one` 和 `format.minutes.other`；中文那边停在 `format.minutes`。
**于是差集里凭空多出九条。**

修正判据 —— `{one, other}` 形状视为叶子 —— 之后：

```
zh-TW : 2586 keys | missing=0
en    : 2586 keys | missing=0
ja    : 2586 keys | missing=0
ko    : 2586 keys | missing=0
```

**零缺失。五个语言，2586 个键，完全对齐。**

顺手再查了占位符（`{n}` 这类）跨语言是否一致 —— 也是 **0 处不匹配**。
如果英文写成 `{count} days` 而中文是 `{n} 天`，界面会渲染出字面花括号。

**这是本项目第 14 次「测量工具本身出错」。**差一点就去「修」一个不存在的缺陷，
而那种修复会删掉复数形式、把英文降级成 `{n} minutes` 这种不合语法的写法。

## 三、已有测试比我想的更全

准备把「键完整性」写成新测试时，先读了 `server/tests/test_web_i18n.py`。

它已经有了：

| 检查 | 方法 |
|---|---|
| 键集与源语言完全一致（**多一个也算错**） | `test_same_key_set` |
| 占位符集合一致 | `test_placeholders_match_source` |
| 复数对象必须带 `other` | `test_plural_forms_have_other` |
| 英文不含中日韩汉字 | `test_english_has_no_han` |
| 没有空译文 | `test_no_empty_translation` |
| 代码里用到的字面量键必须存在 | `test_all_literal_keys_exist` |
| 没有重复键 | `test_no_duplicate_keys` |

**我这一轮的「发现」是它早就在防的事。**

**i18n 质量好是有原因的：有测试守着。**这就是「看不见的状态」被补上了的样子 ——
它不显眼，因为坏了就会有人发现。

唯一想到的补充是「动态拼接的键扫不到」。查了一遍：

```bash
grep -rE "t\(\s*[\`'\"][^\`'\"]*\\\$\{" web --include=*.tsx --include=*.ts
```

**零命中** —— 全仓没有动态键。所以这个缺口目前不存在。

## 四、今天的 5xx 归因

```
endpoint              http_status  error_code                error_source  n
/v1/chat/completions  503          no_healthy_account        upstream      7
/v1/chat/completions  503          gateway.application.error gateway       1
```

**7 个是上游侧**（`no_healthy_account`），落在 11-24 窗口内。
按规范 §3.4 属于第二类「上游瞬时失败」，不是发版责任。

`no_healthy_account` 的日趋势：

```
11-19: 50    11-20: 1    11-21: 1    11-22: 1    11-24: 7    11-25: 0
```

**今天 0 次。**11-24 那 7 次之后没有复现。

**1 个是我们的**（`gateway.application.error`），但看细节：

```
request_id   4cec31f0...
t            2026-11-24 19:19:39
release_id   v1.5.56          ← 35 个版本之前
latency_ms   477              ← 快速失败，不是超时
error_stage  (空)
```

这条是**旧版本上的历史记录**，不是当前问题。

## 五、`error_stage` 为空这件事

上面那条记录的 `error_stage` 是空的，看起来像可观测性缺口。查了代码：

`server/main.py:444` 有一段注释专门讲这件事：

> 实测证据：`request_outcomes` 里 133 条 `gateway.application.error`，`error_stage`
> 全为 null；按端点集中在 `/api/stats/*`（控制台轮询下载缓存失效时抛的
> `UpstreamError(..., code='cache_invalidated')`）与 `/api/system/internal-session-verify`。

根因是 `install_handlers(app)` **从来没被调用过** —— 受控错误一路冒到 Starlette，
被兜成 500 并打印 traceback。现在那行调用在 `main.py:449`。

**又一个「早就修好了」。**

## 六、这一轮的产出是什么

诚实地说：**没有代码改动，没有新版本。**

产出是三条结论：

| 结论 | 依据 |
|---|---|
| 那条硬编码**不该改** | 它做的是「比较后端字面量、展示翻译」，符合规范 §2.2 |
| i18n **零缺失、零占位符错配、零动态键** | 两种独立测量（手写脚本 + 已有测试） |
| 今天的 5xx **没有一条是我们的** | 7 条上游侧、1 条来自 v1.5.56 的历史记录 |

以及两条方法论：

**① 差集类检查必须先用已知正确的样本自检。**
如果我先拿 `zh-CN` 和自己比（应该是 0 差异），就能立刻发现拍平函数有问题。
**任何「发现了一堆问题」的结果，先怀疑工具。**

**② 写新测试前先读已有的测试。**
`test_web_i18n.py` 有 40 个用例，覆盖了我能想到的每一项。差点做了一次重复劳动。

## 七、下一轮去哪

巡检的 3 条警告都是真实问题，但性质是运维操作（充值、观察内存），不是代码。

代码侧已知的缺口清单（都记在 `docs/OPS-PITFALLS.md` 与各 STATUS 文档里）：

| 项 | 状态 |
|---|---|
| P0-1 `writer_entry.py` 安装 | 等运维 |
| P0-2 socket 激活 | 工件已备，等运维 |
| 异地备份 | 需要外部存储与凭据 |
| 全量恢复演练 | 需要维护窗口 |
| 首次归档真实运行 | 约 2026-12-13 |
| CI 覆盖缺口 | 需要 `gh auth refresh -s workflow` |
| 高负载重启验证 | 需要真实流量峰值 |
| 跨周观察 | 需要时间 |

**没有一项能在当前条件下由我单方面完成。**下一轮如果仍未出现 P0/P1，
继续做诊断 —— 但要先想清楚「这次的测量可信吗」，再动手。