# WorkBuddy 上游借鉴 · v0.0.7

本记录基于 2026-11-04 前轮已固定的 GitHub 源码调查，不声称这些提交仍是此刻所有仓库的最新 HEAD。原始调查留在本地 `gateway-upstream-research-20261104`，未执行下载来的代码。

| 来源 | 学习点 | 本候选的取舍 |
|---|---|---|
| [workbuddy2api-hub 的 Claude 模板补丁](https://github.com/ardeyouxipianyi/workbuddy2api-hub/commit/4df31cf1a0e7b34c33bae9ad359b97b4c06f877c) | `CLI tool`、弯引号、大小写变体 | 有界字面匹配，保留应用身份，只处理 system/developer；不照搬整段替换或助手历史改写 |
| [WorkBuddy Manager](https://github.com/ithtelab/workbuddy-manager) | 大请求分散、低余额偏好与面板一致性 | 软偏好默认关闭，保留 unknown ≠0、粘性和可用性回退，配置显式接线 |
| [Magpie WorkBuddy 插件](https://github.com/magpie-community/plugins/tree/main/packages/workbuddy) | 小预算不默认高强度推理 | 只停止本地额外注入，不强迫供应商关闭推理；显式控制始终优先 |
| [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) | 派生统计锁忙不拖住业务 | 不照搬统计 checkpoint 跳写到 SQLite 权威账本；本候选没有该同构统计存储，保留写入/回执失败关闭的合同 |
| [jin66god/workbuddy2api-panel](https://github.com/jin66god/workbuddy2api-panel/commit/6f0caf3cd4f777b2f0111d9e32a8ddac15c1aaea) | 同账号刷新合并 | 除进程内去重，补齐 Python/Go 共享内核锁、凭证代次保护和单次失败惩罚 |
| 其他已固定流式实现与协议观察 | heartbeat 不能吞掉首个工具帧，取消不能伪造成正常结束 | 保留既有 SSE 边界解析和真实终态；不因一个心跳样式就替换整个流式模块 |

## 为什么不整仓替换

本项目还有已有用户钱包、不可变账本、主库预算、签名发布和协调排空边界。上游演示反代的“失败就重试”“删掉系统提示词”“跳过锁忙的持久化”“余额读不到当 0”，不能直接用于这些路径。

没有新增 CodeArts / 媒体生成、售卖/补贴风控或高级 Responses 状态服务。派生统计缓存的进一步重构须先有本地 profile 证据，不为了“上游有”而引入新的全局缓存和业务一致性风险。

## 验证口径

实现、Windows 合成测试、Linux 交叉编译、隔离 Linux 执行和生产验收是五件不同的事。最终结果分别写入本地验收报告，不能把上游某个 commit、一次 integrity_check 或候选源码存在，写成“现网已经修复”。
