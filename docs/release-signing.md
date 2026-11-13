# v1.2.9 发布、摘要与运行信任合同

> 当前权威入口：[PRODUCT-RELEASE.md](PRODUCT-RELEASE.md)。本文件说明源码发布与国内受控运行之间的边界；GitHub Release 本身不等于服务器已经切换。

## 先区分源码、产物和运行状态

- `VERSION`、`server/product-release.json`、`web/lib/product.ts`、两份 CHANGELOG 和 `writer-capabilities.json` 必须描述同一个产品版本。
- 源码固定为 `daniei-chen/zhihui-gateway` 的完整提交，不把可移动 `main` 当作安装批准。
- `server/release-provenance.json` 在 GitHub Actions 组装发布包时写入仓库、产品版本和完整 `source_commit`；缺少记录时界面显示“未记录”，不编造 SHA。
- 发布包与容器制品必须提供 `sha256` 摘要。用户核对摘要后再安装；摘要不符即阻断。
- 公开源码不包含服务器私钥、运行数据库、账号或供应商凭据。

## 当前门禁不能降级

Ubuntu CI 执行完整隔离测试、前端类型与构建检查、依赖审计、定制文件哈希合同和运行镜像扫描。**完整隔离测试**出现失败、依赖缺失、内容漂移或门禁输入变化时，发布不得继续。

国内受控实例仍由 root-owned bootstrap 和可信发布器管理。签名或回执时阻断：签名不符、发布身份不一致、`source_commit` 缺失、清单外代码、摘要不符、健康检查不通过，都不能用“旧安装兼容”放行。

受控 Manager 发布可以使用 Ed25519 信任锚；GitHub Actions 产品 Release 使用提交绑定、SHA-256 产物摘要和草稿核对。两条信任链必须分别标记，不能把未签名的 GitHub 产物伪装成受控 Manager 签名版本。

## 发布与回退

生产由可信发布器完成路由屏障、排空、代码切换、身份和健康核验；不要执行裸 `systemctl restart workbuddy-web`，也不要通过旧网页覆盖安装绕过发布器。

代码回退仍使用可信发布器和已核验的旧版本。前端回退必须指向准确的构建摘要和前身；业务数据库、账号、额度、会话撤销和设备授权不随代码回退，数据恢复须采用独立灾备合同。

## 密钥与能力边界

生产私钥只在服务器受保护签名流程中使用，不进入仓库、CI 日志或公开 Release。签名证明输入身份和未授权漂移，不保证业务正确，也不抵御掌握本机 root/Docker 控制权的攻击者。

健康探针只证明短窗口可用，不等于外部互联网 SLO、真实供应商推理验收或离机恢复验证。

