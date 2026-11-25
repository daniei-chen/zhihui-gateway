# 2026-11-25 字段取值分布复核（第 24 轮）

沿用第 23 轮的方法：改任何查询之前，先把字段的**全部取值**列出来，而不是凭见过的样本写条件。

## terminal_state（8 个值）

```
completed            489,363
rejected              69,667
client_cancelled       1,037
upstream_error           932
application_error        562
interrupted              336
transport_error          304
protocol_incomplete        9
```

## error_stage（10 个值）

```
(null)        559,405   ← 成功请求
client            899
upstream          477
admission         340
application       336
stream_read       304
authorization     283
request_body      156
protocol            9
validation          1
```

## account_source（2 个值）

```
timestamp-heuristic   21,865
legacy-unclassified      153
```

## 关于 legacy-unclassified

这个名字容易误读成「历史遗留数据」，实际它是**列的默认值**：

```python
# db.py:834
('request_logs', 'account_source', "TEXT DEFAULT 'legacy-unclassified'"),
```

也就是说：**任何写入时没显式给 account_source 的行，默认落在这个值**。后续由回填逻辑
（`db.py:1499-1529`）用「时间戳就近 + 模型名匹配」猜出账号，命中后才改成
`timestamp-heuristic`。

**停在 `legacy-unclassified` 意味着「回填没命中」**，不是「数据老旧」。

## 回填为什么会有漏

代码里写得很清楚：

```python
# 只挑 `account IS NULL` 的行，避免把已经填对的行改错（宁可漏填，不可填错）
row = query_one(
    'SELECT id FROM request_logs '
    'WHERE account IS NULL AND ts BETWEEN ? AND ? '
    '  AND (mapped_model = ? OR model = ?) '
    'ORDER BY ABS(ts - ?) LIMIT 1', ...)
```

三个保守条件：
1. 只填 `account IS NULL` 的行
2. 模型名必须对得上（空模型名直接跳过）
3. 时间戳只在窗口内就近匹配

**这是有意设计，不是缺陷。**「猜错账号」比「不填账号」危害大得多 —— 错账比缺账更难查。

## 实际漏了多少

只看**推理调用**（排除 `/v1/models` 这种不产生计费的查询）：

```
近 7 天：未归因 65 次 / 推理总数 22,009 = 0.30%
```

按天看：

```
2026-11-12   1650   ← 异常
2026-11-11    167
2026-11-15     48
2026-11-21     31
2026-11-22     22
2026-11-20      8
2026-11-25      3
2026-11-23      1
```

**11-12 单日 1650 次**是一次集中事件，之后逐日下降，当前稳定在个位数。
近 24 小时 3 次。

## 结论：不改代码

三个理由：

1. **回填的保守是对的**。放宽匹配条件能少漏一些，但会把账号填错 —— 计费错误的代价远大于少量漏填。
2. **比例已经很低**（0.3%），且仍在下降。
3. **11-12 那次爆发的原因不在回填逻辑**（否则日均不会只有个位数），更像是上游某段时间没回传账号信息，已经过去。

## `account_source` 值得加进巡检吗

考虑过，但**不加**：0.3% 的未归因是这套保守设计的**正常水位**，加一条长期显示的
「0.3% 未归因」只会变成噪声 —— 就像之前发现 `workbuddy_low_accounts` 那样，
一条永远亮着的警告等于没有警告。

**只有当它明显偏离水位时才值得报**（例如恢复到 11-12 那种量级），而那种情况
在总错误率和上游失败段里已经能看出来。

## 本轮方法上的收获

`terminal_state`、`error_stage` 的取值都在预期之内；`account_source` 多出一个值，
查下去发现是设计使然而非缺陷。

**列全取值这一步本身就能排除大部分假问题** —— 上一轮就是靠它发现容量拒绝的。