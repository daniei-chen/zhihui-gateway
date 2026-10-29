# K25 测量结果（rows=120000，repeat=5）

## 1. 日志列表：EXPLAIN QUERY PLAN（count / list，`<br>` 分隔）

| 查询形状 | base | safe | rewrite | harmful |
|---|---|---|---|---|
| no_filter | `SCAN request_logs USING COVERING INDEX idx_logs_ts`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN request_logs USING COVERING INDEX idx_logs_ts`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN request_logs USING COVERING INDEX idx_logs_ts`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN request_logs USING COVERING INDEX idx_logs_ts`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| realm_cn | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `MULTI-INDEX OR <- INDEX 1 <- SEARCH l USING COVERING INDEX idx_logs_realm_id (realm=?) <- INDEX 2 <- SEARCH l USING COVERING INDEX idx_logs_realm_id (realm=?)`<br>`MULTI-INDEX OR <- INDEX 1 <- SEARCH l USING INDEX idx_logs_realm_id (realm=?) <- INDEX 2 <- SEARCH l USING INDEX idx_logs_realm_id (realm=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN <- USE TEMP B-TREE FOR ORDER BY` |
| realm_global | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_realm_id (realm=?)`<br>`SEARCH l USING INDEX idx_logs_realm_id (realm=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| days | `SEARCH l USING COVERING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| key_id | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_key_id_id (key_id=?)`<br>`SEARCH l USING INDEX idx_logs_key_id_id (key_id=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_key_id_id (key_id=?)`<br>`SEARCH l USING INDEX idx_logs_key_id_id (key_id=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_key_id_id (key_id=?)`<br>`SEARCH l USING INDEX idx_logs_key_id_id (key_id=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| model_like | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| ip_like | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| status_ok | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_status_ts (status>? AND status<?)`<br>`SEARCH l USING INDEX idx_logs_status_ts (status>? AND status<?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN <- USE TEMP B-TREE FOR ORDER BY` |
| status_error | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SCAN l`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `MULTI-INDEX OR <- INDEX 1 <- SEARCH l USING COVERING INDEX idx_logs_status_ts (status<?) <- INDEX 2 <- SEARCH l USING COVERING INDEX idx_logs_status_ts (status>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| realm_days | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `MULTI-INDEX OR <- INDEX 1 <- SEARCH l USING INDEX idx_logs_realm_id (realm=?) <- INDEX 2 <- SEARCH l USING INDEX idx_logs_realm_id (realm=?)`<br>`MULTI-INDEX OR <- INDEX 1 <- SEARCH l USING INDEX idx_logs_realm_id (realm=?) <- INDEX 2 <- SEARCH l USING INDEX idx_logs_realm_id (realm=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN <- USE TEMP B-TREE FOR ORDER BY` |
| key_days | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_key_id_id (key_id=?)`<br>`SEARCH l USING INDEX idx_logs_key_id_id (key_id=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_key_id_id (key_id=?)`<br>`SEARCH l USING INDEX idx_logs_key_id_id (key_id=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_key_id_id (key_id=?)`<br>`SEARCH l USING INDEX idx_logs_key_id_id (key_id=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` |
| status_days | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING INDEX idx_logs_ts (ts>?)`<br>`SCAN l <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | `SEARCH l USING COVERING INDEX idx_logs_status_ts (status>? AND status<?)`<br>`SEARCH l USING INDEX idx_logs_status_ts (status>? AND status<?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN <- USE TEMP B-TREE FOR ORDER BY` |

## 1b. 改写后的等价谓词（候选，未应用到源码）

| 谓词 | 计划 count | 计划 list | count ms | list ms |
|---|---|---|---|---|
| realm_cn_coalesce | `SEARCH l USING COVERING INDEX idx_logs_realm_cn_id (<expr>=?)` | `SEARCH l USING INDEX idx_logs_realm_cn_id (<expr>=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | 2.74 | 0.146 |
| realm_global_coalesce | `SEARCH l USING COVERING INDEX idx_logs_realm_cn_id (<expr>=?)` | `SEARCH l USING INDEX idx_logs_realm_cn_id (<expr>=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | 0.912 | 0.18 |
| realm_days_coalesce | `SEARCH l USING INDEX idx_logs_realm_cn_id (<expr>=?)` | `SEARCH l USING INDEX idx_logs_realm_cn_id (<expr>=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | 25.869 | 0.204 |
| status_ok_expr | `SEARCH l USING COVERING INDEX idx_logs_status_class_id (<expr>=?)` | `SEARCH l USING INDEX idx_logs_status_class_id (<expr>=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | 3.17 | 0.186 |
| status_days_expr | `SEARCH l USING INDEX idx_logs_status_class_id (<expr>=?)` | `SEARCH l USING INDEX idx_logs_status_class_id (<expr>=?) <- SEARCH k USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN` | 26.158 | 0.209 |

## 2. 统计查询计划

| 查询 | base | safe | rewrite | harmful |
|---|---|---|---|---|
| throughput | `SEARCH request_logs USING INDEX idx_logs_ts (ts>?) <- USE TEMP B-TREE FOR GROUP BY` | `SEARCH request_logs USING INDEX idx_logs_ts (ts>?) <- USE TEMP B-TREE FOR GROUP BY` | `SEARCH request_logs USING INDEX idx_logs_ts (ts>?) <- USE TEMP B-TREE FOR GROUP BY` | `SEARCH request_logs USING INDEX idx_logs_status_ts (status>? AND status<?) <- USE TEMP B-TREE FOR GROUP BY` |
| failure_counts | `SEARCH request_logs USING INDEX idx_logs_ts (ts>?)` | `SEARCH request_logs USING INDEX idx_logs_ts (ts>?)` | `SEARCH request_logs USING INDEX idx_logs_realm_cn_id (<expr>=?)` | `SEARCH request_logs USING INDEX idx_logs_realm_cn_id (<expr>=?)` |
| usage_health | `SEARCH request_logs USING INDEX idx_logs_ts (ts>? AND ts<?)` | `SEARCH request_logs USING INDEX idx_logs_ts (ts>? AND ts<?)` | `SEARCH request_logs USING INDEX idx_logs_ts (ts>? AND ts<?)` | `SEARCH request_logs USING INDEX idx_logs_ts (ts>? AND ts<?)` |
| account_backfill | `SEARCH request_logs USING INDEX idx_logs_ts (ts>? AND ts<?) <- USE TEMP B-TREE FOR ORDER BY` | `SEARCH request_logs USING INDEX idx_logs_unfilled_ts (ts>? AND ts<?) <- USE TEMP B-TREE FOR ORDER BY` | `SEARCH request_logs USING INDEX idx_logs_unfilled_ts (ts>? AND ts<?) <- USE TEMP B-TREE FOR ORDER BY` | `SEARCH request_logs USING INDEX idx_logs_unfilled_ts (ts>? AND ts<?) <- USE TEMP B-TREE FOR ORDER BY` |

## 3. 代表性查询 P50 耗时（ms）

| 查询 | base | safe | rewrite | harmful |
|---|---|---|---|---|
| count:no_filter | 0.015 | 0.015 | 0.016 | 0.019 |
| list:no_filter | 0.14 | 0.143 | 0.141 | 0.141 |
| count:realm_cn | 23.387 | 23.118 | 23.981 | 4.992 |
| list:realm_cn | 0.144 | 0.156 | 0.15 | 164.323 |
| count:days | 0.948 | 0.951 | 0.993 | 0.943 |
| list:days | 0.202 | 0.146 | 0.151 | 0.198 |
| count:key_id | 19.336 | 0.01 | 0.01 | 0.01 |
| list:key_id | 0.386 | 0.149 | 0.146 | 0.157 |
| count:model_like | 26.952 | 26.448 | 27.308 | 28.277 |
| list:model_like | 0.158 | 0.167 | 0.149 | 0.17 |
| count:realm_days | 442.557 | 443.436 | 446.67 | 30.117 |
| list:realm_days | 0.151 | 0.157 | 0.149 | 164.277 |
| count:status_days | 452.814 | 448.428 | 443.469 | 3.914 |
| list:status_days | 0.285 | 0.15 | 0.166 | 430.786 |
| stats:throughput | 829.497 | 503.549 | 504.753 | 465.684 |
| stats:failure_counts | 796.267 | 469.511 | 29.668 | 31.076 |
| stats:usage_health | 465.699 | 445.961 | 456.469 | 463.742 |
| stats:account_backfill | 466.336 | 179.14 | 177.276 | 186.572 |
| ann:no_filter@0 | 0.144 | 0.143 | 0.158 | 0.146 |
| ann:no_filter@2000 | 0.314 | 0.316 | 0.3 | 0.348 |
| ann:no_filter@50000 | 10.584 | 10.695 | 12.052 | 13.595 |
| ann:key_id@0 | 0.392 | 0.152 | 0.151 | 0.229 |
| ann:key_id@2000 | 19.502 | 0.071 | 0.058 | 0.098 |
| ann:key_id@50000 | 21.792 | 0.09 | 0.06 | 0.099 |
| ann:realm_cn@0 | 0.195 | 0.147 | 0.149 | 162.076 |
| ann:realm_cn@2000 | 0.464 | 0.432 | 0.428 | 212.463 |
| ann:realm_cn@50000 | 17.515 | 16.504 | 17.869 | 279.066 |

## 4. 写入与建索引代价

写入代价按「该变体索引集合全部存在时」测量（每千行毫秒，越小越好）：

| 索引集合 | ms/千行 | 行数 |
|---|---|---|
| base | 8.17 | 50000 |
| safe | 9.93 | 50000 |
| rewrite | 12.51 | 50000 |
| harmful | 14.35 | 50000 |

| 索引 | 建索引耗时（ms，已有数据） |
|---|---|
| idx_logs_key_id_id | 66.6 |
| idx_logs_unfilled_ts | 41.2 |
| idx_logs_realm_cn_id | 59.7 |
| idx_logs_status_class_id | 51.3 |
| idx_logs_realm_id | 62.6 |
| idx_logs_status_ts | 77.7 |

JSON 已写入 design\manager\k25-bench.json
