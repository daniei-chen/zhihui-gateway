# 2026-11-25 补充诊断：归档机制复核（第 20 轮）

上一份记录（`STATUS-2026-11-25-evening.md`）写「归档从未运行过」，**该结论有误**，
本文更正。

## 更正

**不是「归档没工作」，是「没有数据需要归档」。**

`request_logs` 实际时间跨度：

```
最老 2026-09-14 20:30:37
最新 2026-11-25 11:53:41
共 368,702 行 / 72 天
```

保留期 90 天。查询到期行：

```sql
SELECT count(*) FROM request_logs WHERE ts < strftime('%s','now')-90*86400;
-- 0
```

**没有任何行超过保留期**，`archive_runs` 为空是正确的空状态。

## 机制本身是完整的

`db.py:1404` 的清理入口并不删除任何东西：

```python
def _prune_request_logs() -> None:
    """按保留期清理请求日志（滚动删除最旧的），顺手清理侧表。"""
    try:
        from . import archiveworker
        archiveworker.notify()      # 通知归档 worker
    except Exception:
        pass
    _prune_side_tables()
```

治理工具在 `retention.py`（`archive_dataset` / `delete_archived`），
测试在 `test_retention_archive.py`（覆盖导出、二次归档幂等、CLI 入口）。

**链路完整。**

## 顺带查清的历史问题

`request_logs` 中 `request_id IS NULL` 共 126,883 行（34%），按月分布：

| 月份 | NULL | 总行 | 占比 |
|---|---|---|---|
| 2026-09 | 101,016 | 101,016 | 100% |
| 2026-10 | 25,867 | 143,476 | 18% |
| 2026-11 | 0 | 124,211 | 0% |

最近 7 天与 24 小时均为 0。**当前代码不产生此类行**，历史行随 90 天滚动清除。

## 容量评估

```
5,121 行/天 → 稳定态（90 天）≈ 460,000 行 ≈ 155 MB
```

库总量 718 MB，其中不可删账本占大头（`forwarding_events` 105 MB 等）。
`request_logs` 稳定后不再增长。根分区剩余 17 G，不构成风险。

## 教训

「没有记录」必须先问「应该不应该有记录」。空表可能是坏了，也可能是正确地无事可做。
区别在于去找**输入**（有无到期数据），而不是只看**输出**（有无运行记录）。