# 外置授权与回调工具补审

基线 `4c6b4cb8cb790698e0753909745dfe8a7c78dd9f`。指定 6 个生产工具文件共 **1,360 行，全部完成全文人工阅读**。另全文阅读两份 README、一个 systemd unit、两份 Nginx 配置，以确定真实部署与使用边界。逐文件范围和 SHA-256 在 `ops-tools-extra-reviewed.tsv`。本轮只读源代码；未运行浏览器、SMS、授权、SSH、systemctl、Nginx 配置变更或远端部署。

## 结论

没有确认新的 P0/P1。这里有两个不同实验边界，不能混成一条已经部署的公网授权链路：

- `authorization-worker` 的 broker 是严格固定 loopback fixture 的私有实验 transport；stdio 没有生产 launcher、HTTP 或 CDP 监听，返回 `productionReady:false`。检索 `manager/server` 未发现生产模块导入它。session 类包含 live official controller 能力，但 README 明确缺少已验证的 Linux sandbox/外部生命周期监管/生产 transport；不能以 Manager 全量通过声称它已可生产使用。
- `trae-callback-probe` 是固定现有域名的 HTTPS 回调到达实验。它可以通过现有 Manager 的会话接口 start/review/cancel，但没有 finish/exchange 方法；回调页面明确只证明 trace 和格式匹配，不证明授权码有效或账号入池。

## 按文件核查

| 文件 | 实际全文核查要点 |
| --- | --- |
| authorization-worker/broker.cjs | 全量命令字段白名单、owner/state/随机 handle 绑定、常量时间比较、fixture origin/PKCE bootstrap 校验、容量在 await 前占用、迟到 context 清理、operation deadline、隔离失败 slot 不提前释放、输出字段与图像上限、terminal retention、shutdown。 |
| authorization-worker/session.cjs | 官方 origin 与资源名单、固定 loopback 回调拦截、主 frame/GET/trace/一次性约束、无实际 loopback请求、允许的有限动作、输入/帧的身份和过期、页面导航/尺寸变化、pointer释放、速率限制、上下文关闭与异常状态。 |
| authorization-worker/stdio.cjs | 严格 UTF-8/JSON、24 KiB 行/48 KiB单次输入、响应上限、单行串行执行、写回压与超时、断管/EOF shutdown、不反射异常或回调秘密。 |
| trae-callback-probe/server.py | 固定 Manager 方法与地址、cookie/Origin/Host、本人角色及授权代次复查、PKCE/trace/bootstrap限定、随机测试能力、同步锁与8会话上限、一次性回调、代码只在内存校验后丢弃、输出HTML转义、安全响应头、线程槽与socket超时。 |
| trae-callback-probe/deploy_remote.py | 固定目标/文件名单、closure与现有字节hash、协作发布锁、独占nofollow写入+fsync、运行用户、unit验证、回环自检、Nginx变更意图/回执、发生漂移拒绝覆盖、失败回退。 |
| trae-callback-probe/rollback_remote.py | 固定实验回执/前后Nginx字节、一致性/链接校验、独占临时文件替换、删除限流配置、reload/停止实验服务、回执与父目录fsync；没有恢复业务数据库或用户文件。 |

## P2 残余及证明边界

### 1. 特权实验部署用 assert 承担运行时拒绝条件

deploy_remote.py 与 rollback_remote.py 使用 assert 校验 uid、host closure 摘要、输入文件闭包、内容摘要、原始 Nginx/页面、端口、service状态和回滚前的精确代次。解释器优化模式会去掉这些 assert。文档命令使用 `python3 -I -B`，未启用 `-O`，因此不能声称按文档正常运行就绕过校验；但安全拒绝条件应使用显式条件与异常，避免未来 launcher 的解释器选项改变其语义。

部署文件 payload 的摘要由同一份 stdin 提供，属于传输完整性，不是独立发布者身份认证。这里仍需要可信操作者/传输，以及私有 writer-guard 的授权边界。公开仓库没有完整远端 writer-guard 和 sudo/SSH 策略，不能在本地证明这些特权部署入口的调用权限，也不能据同源摘要断言私有发布器已安全。

### 2. Nginx 原子替换成功、后续目录 fsync 失败的回退窗口

deploy_remote.py 的 durable(..., replace=True) 先 os.replace，后父目录 fsync；main 在函数完全返回后才设置 changed=True。若 replace 已成功而目录 fsync 抛错，except 路径仍按 changed=False 处理，可能不恢复已经替换的站点文件。后续删除 rate 文件会使磁盘上的 candidate 引用不存在的 limit zone，nginx -t 失败又会打断后面的 stop/回执处理。

这是从实际控制流可定位的运维故障窗口，不是已经在真实远端触发的事件。建议把已提交替换与耐久性失败分别记录，并在受控异常路径按已记录的精确 before/after 字节复核磁盘状态，不能仅依赖“函数是否成功返回”的布尔值。

### 3. 回滚过程不是可在任意中断点重入的完整状态机

rollback_remote.py 在替换 Nginx、删 RATE 后才验证和 reload。中途命令失败时，可能已处于 original ingress / RATE缺失的部分回滚状态；重跑入口又要求 Nginx 必须等于 after 且 RATE存在，因此会拒绝自动继续。部署失败路径保留 unit/证据也属于明确的残余状态。应在回执中声明各中断阶段，允许经过精确代次校验的幂等续跑；不要简单删除校验来“修复重试”。

### 4. 浏览器实验的资源隔离仍依赖外部监管

Promise.race 的 JavaScript deadline 不会证明 native browser process 已退出；session.shutdownBrowser 会吞 context.close 的错误，broker 依靠实际 close 事件隔离槽位并报告 shutdown_unconfirmed。README 已明确这些限制。Playwright route allowlist 也不是操作系统出站隔离，不涵盖所有 browser transports；此项是已有生产启用门槛，不应重复计作新的匿名远程漏洞。

### 5. callback probe 的安全承诺应保持当前范围

它验证回调格式和同一 Manager 授权状态，并不向 provider 兑换/验真 AuthCode；具备本人 cookie 与本次能力的人能够提交格式匹配的合成回调，页面仍只称“匹配回调到达”，当前文案准确。敏感URL的普通访问日志被关闭，但 Nginx在 location匹配前拒绝的畸形请求仍取决于宿主底层日志策略；README已披露。真实宿主Nginx原字节、两处 server_name 所属listener与私有发布锁实现均不在公开源码中，因此没有声称验证线上安装位置或生产回调成功。

## 交接

本次没有更改这 6 个工具及其配置。它们在原 ops 扫描清单中曾是 automated，现在通过独立清单明确补为 full；不要继续把它们算作“仅扫过”，也不要把整个 tools 目录顺带升级成全文覆盖。

主代理后续授权修复的是 keyimport/notify 两个独立模块，与本工具只读补审分开记录；不扩展到这些特权部署脚本或浏览器实验。
