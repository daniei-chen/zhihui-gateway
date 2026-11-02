# 受控升级入口收口（本地候选，未发布）

这批修改只缩小遗留自更新入口，不更改产品版本，不重新安装宿主发布工具或信任锚。

## 行为

- 存在 `deploy/managed-release.json` 时，网页 `POST` / `DELETE /api/system/update-package` 返回 409，不覆盖暂存包、不删除历史包或元信息；`GET` 继续允许原有已认证角色读取。
- 元信息的 `present` 只表示文件存在。受控模式固定返回 `upload_enabled=false`、`installable=false`、`publisher_verified=false` 与 `update_policy=trusted-publisher-required`，不采信数据库里同名的批准状态。
- 网页更新与旧 CLI 继续拒绝；直接调用旧 worker 的 `update_manager`、`update_upstream`、`_release_root` 也先检查受控标记，包括缓存命中之前。
- 标记内容损坏、标记为目录、断链、读取权限错误不自动恢复旧写入口。真正不存在时保留历史非受控安装兼容性。标记及父目录应由受信发布者保护；这不是对拥有宿主写权限者的隔离边界。
- 控制台受控、身份未读到或两个身份响应冲突时，上传、摘要输入、清包和安装均禁用；只有明确一致的非受控身份保留旧控制。
- `tools/verify_signed_release.py` 已退役，始终返回退出码 2 / `status: RETIRED`；不读取公钥、manifest、签名或压缩包，不再输出发布批准。
- 历史非受控上传使用独占随机临时文件，取消和写入失败只清理本操作临时文件，不清除其他上传或旧残留。

HTTP multipart 解析可能先于 handler 消费请求体并使用框架临时文件。本批“不写包”指不创建应用正式包及其暂存文件，不等于上传流量不会抵达框架，也不是请求体资源治理的完整交付。

## 为什么不继续修补旧安装器

受控实例实际由独立受信发布器安装，不需要网页暂存链路。不能为了给坏包补检查而重新开放旧安装器，也不把上传者提供的正确 SHA 当成可信发布者签名。

签名、完整成员集合、来源与精确输入测试回执仍由已安装的独立受信发布器验证。本批没有创建替代发布器，也没有证明那个发布器的新故障场景。

## 未关闭项

- 非受控历史上传仍未具备完整 archive / CRC / 重复路径 / 展开资源扫描；rename 后元信息失败的一致性问题仍未修复。
- 非受控旧更新器仍不保证后端、前端、依赖与版本标记共同原子，不应作为受控回退方案。
- 真实发布及回退演练尚未运行。回退必须只切换兼容代码，不能恢复旧数据库、用户、密钥、Token、权限、额度或通知收件人。
- Windows 不能创建符号链接时，相应真实文件语义测试保留为跳过；Linux 完整门禁仍需补齐。

## 本地验证

新增测试：`manager/server/tests/test_v004_managed_update_boundary.py`、`src/three-platform/web/tests/managed-update-boundary.test.cjs`。

前端新增测试执行：

```sh
cd src/three-platform/web
node --test tests/managed-update-boundary.test.cjs
```

全部证据与实际计数在仓库外本地交付报告中记录，不把本批候选写成生产已部署。原有 IP 使用规则、额度、业务权限和供应商目标都未调整。
