# 安全说明（笔记）

> 从 README 拆出来的一节，内容不变；另见 [安全审计报告](SECURITY-AUDIT.md)。
> 这里讲的是**本项目的安全设计**，不是漏洞上报流程（那属于仓库的 SECURITY 政策，需要时另建）。

- 网关密钥仅存 SHA-256 哈希，明文只在创建时返回一次
- 管理端密码使用 PBKDF2-SHA256（26 万次迭代）加盐存储
- 会话使用 HttpOnly + SameSite=Lax 签名 Cookie，生产环境自动启用 `Secure`
- **写端点的同源校验锚点在服务端**（`WB_PUBLIC_ORIGIN`，其次 `WB_CORS_ORIGINS`）：
  2026-11-28 起不再用请求的 `Host` 拼「本站期望来源」—— DNS rebinding 下
  `Host == Origin == 任意域名`，那样判等于没有锚点。默认部署行为不变：IP / localhost
  / 单标签内网名直连、经可信反代（`WB_TRUSTED_PROXY_CIDRS` 透传
  `X-Forwarded-Host`）、以及配了来源的部署照旧。只有「用域名访问且没配
  `WB_PUBLIC_ORIGIN`」这一种组合的表单提交会 403（body
  `{"detail": "origin_unconfigured"}`）：管理页会在填表之前显示横幅说明怎么修，
  启动日志也告警一次。「安全」页的状态栏显示这次访问路径的来源状态。逃生门
  `WB_ALLOW_UNCONFIGURED_ORIGIN=1` 可退回修前行为，但会重新暴露 DNS rebinding，
  启动日志会醒目告警
- 同 IP 登录失败 5 次锁定 10 分钟
- 所有文件操作做路径穿越校验
- **真实 IP 取自反代覆盖写入的 `X-Real-IP`**（`X-Forwarded-For` 首段可伪造），
  且**只在 TCP 对端属于可信代理网段时才采信**；默认可信网段只有回环
  （`127.0.0.0/8`、`::1/128`）。反代在另一台机器 / CDN 回源时，用
  `WB_TRUSTED_PROXY_CIDRS` 显式添加，否则同网段的任意主机都能伪造来源 IP
- 登录失败**按 IP + 用户名双维度锁定**，防单机与换 IP 的分布式爆破
- 管理面支持**作用域化 API Token**（只读 / 管理员，可吊销、可过期，库中仅存哈希、
  全程审计），供脚本 / CI 免登录调用；**高危接口与令牌管理本身只接受会话登录**，
  令牌泄露也无法提权或自助持久化（见 [docs/api-tokens.md](api-tokens.md)）
- 生产环境默认关闭 `/docs`、`/openapi.json`（`WB_ENABLE_DOCS=1` 开启）
- 网关限制请求体大小（8 MiB）与每密钥调用频率（默认 120 次/分钟）
- 已配置 CSP、`X-Frame-Options`、`X-Content-Type-Options` 等安全响应头
- `users.json`、`data/*.db`、`.env`、账号授权文件均已在 `.gitignore` 中排除
- **更新链路**：发布包必须通过发布页 sha256 校验才安装（拿不到摘要默认拒绝，
  Windows 更新器需显式 `-AllowUnverified` 才放行）；包先解压到临时目录并校验
  关键产物，再整体替换，失败回滚；更新日志中的代理凭据会打码，只读账号看不到日志
- **审计与任务日志有保留上限**（审计 2 万行、任务日志 90 天、小时统计 90 天），
  避免长期运行或未认证探测把库撑大

> 完整审查结论见 [安全审查报告](SECURITY-AUDIT.md)（含已修复的高危问题与验证证据）。

### 已知的统计口径

- 用量按**本地时区**归日（写入、回填、展示统一口径）。历史上写入用本地、
  回填用 UTC，会在 UTC+8 的机器上把凌晨的调用算成两天；如你的数据受此影响，
  可在「用量 → 重建统计」以请求日志为准重建一次

### 安全建议（部署后）

1. **改掉初始密码**，不要沿用部署脚本中的默认值
2. **务必经 HTTPS 访问**：7863 / 7864 建议只监听 `127.0.0.1`，由反向代理对外
3. 如需前置 CDN，请把 `WB_TRUSTED_PROXY_HOPS` 设为 CDN + 反代的层数
4. 发现问题请走[私密渠道](https://github.com/daniei-chen/zhihui-gateway/security/advisories/new)，
   **不要**公开提交 Issue
5. **更新源必须是你显式指定的 owner**：`deploy/update.py` 的 `ALLOWED_MANAGER_REPOS` 与
   `windows/update.ps1` 的 `AllowedManagerRepos` 都只接受白名单内的仓库（默认
   `daniei-chen/zhihui-gateway`），不在白名单会**直接中止**而不是回退到默认源。
   更新器以装机权限执行包内的依赖清单，把发布源指向别处等于把「装什么」的决定权
   交出去；要自建镜像请改白名单。
   两侧的完整性校验**不一样**：Linux 侧除摘要外还要求一次 **Ed25519 发布签名**
   （`deploy/update.py` 的 `verify_release_signature`）；Windows 侧目前**只核对
   sha256**，而该摘要与包同源，能控制发布源的对手可以一并改掉

> ⚠️ 公网暴露**必须**启用 HTTPS，否则会话 Cookie 与密码可被中间人窃取。
> 建议再叠加 1Panel IP 白名单或 Cloudflare Access 加固。

---
