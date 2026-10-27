# K04/K05：Manager → Nginx → QTG 身份链候选

范围：本地实现与隔离核验，未部署生产。对应实际代码，而非旧交接的完成声明。

## 1. 已复现的缺口与修正

1. 旧 QTG 管理员 Cookie 优先于 Manager 的当前 viewer 断言，可能覆盖降权后的身份。
   经验证的 Manager 签名身份现优先；坏签名、缺失必要断言均不能退回本地 Cookie。
2. scopes 原先只有记录，没有参与授权；现与角色、动作、平台和真人会话要求取交集。
3. 浏览器 wb_session 失效时，代理注入的裸内部凭据会退为 internal_service。现拒绝该降级。
4. 业务成功审计常量 actor=admin/compat。现用请求 ContextVar 在 Runtime.record_audit
   统一补充已验证 actor、identity、request_id；请求外调度器保留自己的 actor。
5. Manager scope=keys 原先可读许多 viewer 端点。现仅允许实际 key CRUD/reset、me、身份探针。
6. QTG 密钥列表硬编码三平台，掩盖 Manager 的实际限制；现回传权威 platforms，缺失时不扩大。
7. 旧候选把机器服务令牌作为身份签名密钥，也允许机器凭据加明文用户头自称真人。
   现独立配置身份密钥，禁止与机器令牌相同；不接受明文用户断言。裸机器令牌仍只代表机器。

红测见 evidence/identity-chain-red2.log；该轮 8 失败/1 通过（读依赖的原夹具未开启 read_requires_auth，
随后专项已开启并补验）。不能把首次自定义路由被 SPA 吞掉的夹具问题算产品漏洞。

## 2. 请求链和职责

1. Nginx 受保护 /api/ 请求先对内部 /__zh_identity 执行 auth_request。
2. 子请求携带原 Cookie/管理 Token 到 Manager GET /api/authz/probe。该端点只认证；
   复用唯一 current_user 入口，重查当前用户、角色、会话版本或 token 启用/有效期。
3. Manager 成功返回短期 X-QTG-Identity 和 no-store；不读取业务设置/密钥、无推理调用。
   Token last_used 的节流更新与会话续期仍按原身份服务逻辑执行。
4. Nginx 覆盖签名头，清除客户端全部身份/内部凭据/Authorization/X-API-Key/伪造 marker，
   将 X-QTG-Identity-Required=1 写给 QTG；转发子请求 Set-Cookie，保留滑动续期。
5. QTG 验证真实边缘对端、独立 HMAC、aud/exp/role/auth_method；再对实际 method/path 授权。
   Nginx 的认证成功不代替 QTG 的写操作授权。身份桥接要求 authz_enforce=1，否则 503。
6. 所有后端写入仍必须做自己的参数与业务校验。Cookie 写请求仍校验完整 Origin，签名不豁免 CSRF。

Nginx 候选：ops/nginx/qtg-identity-bridge.conf，server 上下文片段。
必须合并现有生效 vhost，保留真实公开 Manager 登录/退出/me 位置与推理路由；不允许整文件覆盖。
QTG 的 X-Forwarded-For 清空、外部客户端地址仅用 Nginx 覆盖的 X-Real-IP 记审计。
否则 Uvicorn 默认代理头中间件会把 scope.client 替换成外部 IP，身份解析器失去真实边缘对端。
实际核验启用了 Uvicorn proxy_headers=True，不依赖测试里额外关闭它来掩盖部署差异。

## 3. 断言与权限

格式：v1.base64url(canonical UTF-8 JSON).base64url(HMAC-SHA256)。
签名派生键为 SHA256(ASCII 前缀 qtg-identity-v1 + 一个 NUL 字节 + UTF-8 编码的独立身份密钥)，兼容 v1 格式。
sub、role、scopes、auth_method、aud=qtg、iss=manager、iat、exp=iat+60、随机 jti。
中文用户名保留在 UTF-8 载荷中，HTTP 头本身仅 ASCII。断言不是业务推理密钥。

| Manager 凭据 | QTG sub | role | scopes | auth_method |
|---|---|---|---|---|
| 当前管理员会话 | 实际 username | admin | * | edge_session |
| 当前只读用户会话 | 实际 username | viewer | read | edge_session |
| admin scope Token | manager-token:稳定id | admin | * | admin_token |
| readonly Token | manager-token:稳定id | viewer | read | admin_token |
| keys 服务 Token | manager-token:稳定id | internal_service | keys:read,keys:write | admin_token |

keys:write 仅覆盖实际 Manager key 创建、数字 id 更新/删除/reset-usage，以及对应 QTG 创建入口；
不授予账号、日志、设置、metrics、任意 actions 或真实用户会话。role 仍是另一道上限。
read 不授予写操作/GET 动作面或管理员专有敏感读取。未知 scope 拒绝；空 scope 仅兼容 QTG 本地旧会话。
裸 internal_service 的旧 service scope 保留原普通机器能力，真人专用操作仍拒绝。
探针可按指定目标给出同一授权决定；identity/probe/matrix 自省不扩大业务权限。

## 4. 凭据配置与故障边界

- Manager：WB_QTG_IDENTITY_SECRET_FILE，默认空，未配置/弱值/非普通文件/软链/公开 POSIX 权限返回 503。
- QTG：QTG_IDENTITY_SIGNING_KEY_FILE，默认空（只关闭跨服务签名，不影响独立本地登录）。
  文件同样要求普通文件、0600、至少 32 字符且不含空白，不能等于 internal_service_token。
- 两个服务使用相同的独立随机密钥材料。分属不同服务账户时，各持独立 0600 副本，不能为了共享改成全局可读。
- Nginx 不持有该密钥，也不再给每个浏览器请求注入具有管理权限的静态 Bearer。
- 签名文件每次重新读取，支持原子替换后生效。两端不一致时拒绝，不回退到弱凭据。
- 当前尚未实现双 kid 的重叠轮换窗口；顺次替换两个副本期间会拒绝认证。
  “无需重启”不等于“轮换零中断”。生产 K17 轮换必须补重叠验证或先确定可接受维护策略。
- 应用监听/防火墙/同主机权限仍属于 K08/K16/K20；可信回环不能替代文件与进程权限隔离。
- 现有 Header exp 校验沿用 QTG 60 秒时钟偏差容忍；边缘每次实时认证且清掉客户端断言，不能把短期断言当客户端 API Token。

## 5. 核验与尚未闭环

tests/linux_lab/verify_identity_bridge.py 仅允许 zhihui-local-lab /home/gateway-lab，
独立 DB/用户/密钥/配置、真实 Manager 与 QTG 进程、真实 Nginx/TLS；仅假推理 provider。
覆盖真实 viewer 列表、角色降级/吊销、旧本地 Cookie、伪造头、CSRF、续期 Set-Cookie、
Token 类别/高危操作、密钥创建后 Manager DB 平台/配额回读、QTG 审计归因、错配/丢失签名密钥。
另增加裸机器凭据不能签名/明文声明真人的直接真实进程核验。
各次 result.json 绑定源包 SHA，后续修正不能冒用前一份成功结果。

仍未完成：

- 浏览器 26 页/209 交互 × 三平台/角色的完整矩阵；不把 API 链和静态 HTML 当页面验收。
- 所有端点的敏感读取分类与契约差异逐条复核；K04 不因一组用例通过直接关项。
- QTG 调用 Manager 的下游 key 写入，目前 Manager 审计主体仍是 scope=keys 的服务 Token；
  QTG 审计保留真实发起人。需受限委派/可验证关联来补 Manager 侧完整人类归因，禁止信任裸 x-internal-actor。
- 全局错误/部分失败信封、主辅会话兼容、全部退出与旧标签页矩阵，仍归 K03/K05/K29/K35。
- 最终统一候选 Linux 全量、生产配置脱敏预检、签名安装/回退和生产发布。
- 最短停机仍需预算 owner/fencing、替换实例准入交接及实测。此身份链实验不测停机时长。
