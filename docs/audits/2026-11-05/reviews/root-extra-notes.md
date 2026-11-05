# 主审补充记录

本记录补充逐文件清单中的 CLI、历史验收工具和本次测试回执修改。未执行真实数据库归档、删除、迁移、systemd 或部署操作。

## 回执准确性修复

`tests/zhihui_harness/pytest_plugin.py` 原先以最多 300 条样本的长度表示网络总次数，同时把包含 loopback 的观察称为“非回环尝试”。回环压力测试填满样本后，后续拒绝访问的目的地主机不再出现于样本。fake provider stats 的最后获取发生在构建字段之间，样本列表引用继续增长而计数已经固定。

本次引入独立总计数、独立有界非回环样本、测试 ID、截断标志，以及 stats 获取后的列表快照。路径保护记录也附带测试 ID。两条行为回归修前均失败、修后通过；既有六条保护合同保持通过。默认 block 的拒绝行为未改变；warn/off 仍按明确参数执行。这个插件是 Python 进程内拦截，不声称能够约束任意 native 子进程的网络。

最后独立根工具全量 `python -B -m unittest discover -s tests -p 'test_*.py' -v` 为 73/73 通过。它包含本次新增的两条回执回归，不是 Manager 的完整 pytest 结果。

## 本机工具阅读

- `tools/archive_ledgers.py`：归档核心已由 Manager 全文审计与回归覆盖，此文件为 CLI 薄壳。查看默认 dry-run 选项、dataset 枚举、摘要确认和归档后删除调用；本次未运行归档。生产路径守卫只识别硬编码的三类前缀，且不保护所有输出参数；它不是任意部署位置的操作系统安全边界。输出错误路径和损坏参数也不保证结构化失败，但未发现新的默认公网执行入口。
- `tools/metering_reconcile.py`：使用已有只读查询计算差异，保留 unknown，并用非零退出码呈现需要人工处理的发现。SQLite 连接没有设置 URI mode=ro/query_only，所谓只读是调用逻辑的约定而非连接权限证明；使用本地副本的要求仍必要。未把正常 SELECT 导致的连接行为误报为已经改动生产账本。
- `tools/migrate_qtg_keys.py`：仅加入受控 Manager import path 并调用已经全文阅读的 legacymig.cli。未执行迁移。
- `ops/systemd/qtg-decouple/install.sh`：历史 root 运维入口，有备份、确认结果和特定失败回退；没有执行。`sed` 删除整条含 QTG 的 PartOf 行，若同一行还有其他依赖也会一并移除；`set -e` 的所有失败点并没有通用 trap 回滚。backup/dropin 名称和 JSON 拼接假定可信管理员输入，不能把说明中的自动回退理解成任意中断均原子恢复。

## 历史状态 lint 不属于本候选验收

`tools/status_lint.py` 共 917 行已完整阅读。它核对旧 R0–R3 台账的固定数字、路径存在和声明文字，部分内容允许历史条件通过，且不重新计算多数被引用证据的源码摘要。它是特定历史记录的自洽性检查，不是本次候选签名或运行证明。

只读执行 `python -B tools/status_lint.py --quiet` 返回 1：公开树缺少 `tasks/status.json`。保留 `evidence/status-lint-audit.log`；没有伪造台账、删除要求或把别的测试通过替代这项结果。本次全新报告与逐文件摘要使用自己的明确基线和最终回执。

## 前端构建辅助文件

补读了生成 `next-env.d.ts`、PostCSS 配置和 `normalize-static-export.cjs`。后者先拒绝输出树中的链接/特殊文件，规范化 RSC 段名，冲突时只有字节相同才删除旧名，否则拒绝。这里操作的是构建输出，不改变 JS 内容；全文阅读不代替原生 Windows 导出的运行验证。
