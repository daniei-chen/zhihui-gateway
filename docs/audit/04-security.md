# D 节：安全 / 鉴权（zhihui-gateway @ ec87de9）

- **范围**：权限矩阵、会话与 Cookie、注入面、凭据与日志、限流与配额绕过、安全相关测试。
- **方法**：只读。静态阅读 + 用本机既有 venv（`D:/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe`）跑既有安全测试 + 用 `fastapi.testclient.TestClient` 发真实请求验证（`WB_DATA_DIR=/tmp/audit-d/data`，不写仓库）。
- **时间**：2026-11-28 01:00–02:00（Asia/Shanghai）。
- **结论**：**没有发现现成的 P0**（没有匿名读取/写入管理面、没有路径穿越、没有 SQL 注入）。发现 1 个「高」级逻辑缺陷（按账号锁定被 IP 维度架空，已实测绕过）、1 个「中」级 CSRF 锚点缺陷（已实测）、以及若干中低危问题。逐条如下。

---

## 0. 权限矩阵（代码 + 实测反推）

人类角色只有两种：`admin`（超级管理员）与 `account_admin`（普通管理员）——`server/userroles.py:15` `HUMAN_ROLES = (SUPER_ADMIN, ACCOUNT_ADMIN)`。另有两类机器凭据：管理 API Token（`api_tokens` 表，scope ∈ `readonly|read|keys|admin`，`server/tokensvc.py:34`）与门户用户会话（`zh-user-portal` Cookie，`server/routers/portal.py:19`）。

| 主体 | 判定入口 | 读 | 写 | 明确被挡 |
|---|---|---|---|---|
| `admin` 真人 | `require_admin` / `require_session_admin`（`server/security.py:1174`、`:1192`） | 全部 | 全部（高危端点要求**会话**，API Token 不可用） | — |
| `account_admin` 真人 | `require_panel_reader` + `userroles.account_route`（`server/userroles.py:88-92`） | `PANEL_READ_ROUTES` 白名单（`server/userroles.py:30-49`） | 仅 `ACCOUNT_ROUTES`（`server/userroles.py:17-28`：登录/扫码发起/授权复核/取消） | 账号导出、密钥导出、设置写入（实测 403） |
| 门户用户 | `portal.current`（`server/routers/portal.py:118`） | 自己的密钥/钱包/账号 | 自己的密钥、自己的授权 | 管理面全部 |
| Token `readonly` | `scope_to_role` → `viewer`（`server/tokensvc.py:54`） | 走 `require_panel_reader` 的读端点 | 无 | `require_admin` / `require_session_*` 全部 |
| Token `read` | `server/security.py:989-1019` 前缀白名单 | `/api/keys`、`/api/me`、`/api/logs`…（`read_prefixes`） | 无 | `/api/tokens`、`/api/users`（`server/security.py:1018`） |
| Token `keys` | `server/security.py:978-988` | `/api/keys`、`/api/me` | `POST /api/keys` | 其余全部 |
| Token `admin` | `scope_to_role` → `admin` | 全部 | 除 `require_session_admin` 外 | 会话专属高危端点 |

实测（干净客户端，无 Cookie，`Authorization: Bearer <readonly token>`）：

```
GET  /api/me              -> 200
GET  /api/tokens          -> 403  需要管理员权限
GET  /api/users           -> 403
GET  /api/accounts/export -> 403
GET  /api/settings/github-token -> 403
GET  /api/keys            -> 200
GET  /api/accounts        -> 200
```

`account_admin` 实测：`GET /api/accounts` 200、`GET /api/accounts/export` 403、`GET /api/tokens` 200（见 D-6）。

---

## D-1【高】管理端「按账号锁定」被 IP 维度架空，分布式撞库可直接绕过（已实测）

- **位置**：`server/security.py:666-679`（`login_blocked`），调用点 `server/routers/auth.py:75`（`_complete_login` 进入时先判锁定）。阈值：`server/security.py:554-556`（`MAX_FAILS=5`、`LOCK_SECONDS=600`）。
- **现象**：账号分支要求「该 IP 也有失败记录」才生效：
  ```python
  # server/security.py:672-678
  if username:
      ucnt, ufirst = _fail.get(...)          # 账号维度
      if ucnt >= MAX_FAILS and (now - ufirst) < LOCK_SECONDS:
          icnt, ifirst = _fail.get(ip, ...)   # ← 还要求同一 IP 有失败
          if icnt > 0 and (now - ifirst) < LOCK_SECONDS:
              return True
  ```
  而 `record_fail` 只在**判定之后**才把该 IP 计数加一（`server/routers/auth.py:75` 先判、`server/security.py:683+` 后记）。因此一个**干净 IP 的第一次尝试永远放行**，即使该账号已被 5 个不同 IP 各试错一次。
- **为什么是问题**：设计意图是「防换 IP 撞同一账号」（代码注释 `server/routers/auth.py:81` 明写「按 IP 与用户名双维度」），实现上账号维度却依赖 IP 维度。攻击者用 N 个出口 IP 各试一次，可把针对 `admin` 的尝试量从「每 10 分钟 5 次」提升到「每 10 分钟 N 次」，且永远不会触发账号锁。`admin` 是唯一超级管理员账号（`server/userroles.py:...` 迁移要求唯一 admin），一旦被撞开即是全量控制。
- **怎么验**（实测输出，无副作用）：
  ```bash
  cd /d/AI/capy/worktrees/jam_01M8EY6H3B8VWJYY4C33BWWNHD/zhihui-gateway
  PYTHONUTF8=1 WB_DATA_DIR=/tmp/audit-d/data \
    "D:/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe" - <<'PY'
  import sys; sys.path.insert(0,'.')
  from server import security
  for i in range(1,7):
      ip = f'203.0.113.{i}'
      print(ip, 'blocked_before=', security.login_blocked(ip, 'admin'))
      security.record_fail(ip, 'admin')
  PY
  ```
  实测：六个 IP 的 `blocked_before` 全为 `False`（第 2–6 个 IP 在账号已有 1–5 次失败的情况下仍被放行）。
- **修复方向**：`login_blocked` 的账号分支去掉 `icnt > 0` 条件，账号锁独立生效；同时保留 IP 锁作为第二道。这样「换 IP 撞同一账号」才真正被拦。

---

## D-2【高】门户登录的账号锁同样被 IP 架空（`blocked_until` 与内存计数取「与」）

- **位置**：`server/routers/portal.py:165-166`（判定）、`server/routers/portal.py:86-88`（`_portal_user_blocked` 键是 `(ip, username)`）、`server/routers/portal.py:178`（写 `blocked_until`）。
- **现象**：
  ```python
  # server/routers/portal.py:165
  if (user and user['blocked_until'] > store.now()
          and _portal_user_blocked(ip, username)):
  ```
  DB 里的 `blocked_until`（账号级，5 次失败写 600 秒）只有在**同 IP 的内存计数**也命中时才生效。换 IP 后 `_portal_user_blocked` 为 False，账号锁被短路。
- **为什么是问题**：门户是**对外**的用户入口（`server/routers/portal.py:1` 注释「Customer portal sessions are separate from every administrator credential」），账号锁被架空意味着可以对任意已知用户名做分布式撞库；门户账号一旦被撞开，攻击者可拿到该用户的调用密钥（`server/routers/portal.py:331` `/api/portal/keys`）与钱包余额。
- **怎么验**：静态可判（上面两行）。动态验证：用 `TestClient` 以 5 个不同 `X-Forwarded-For`/`Host` 组合（`TRUST_PROXY` 默认 `1`，`server/config.py:196`，但 `TRUSTED_PROXY_CIDRS` 默认只信回环，`server/config.py:213`，所以本机测试需用 `TestClient` 直连并 mock `client_ip`）对同一用户名各试错一次，观察第 6 次是否仍返回 401 而非 429。
- **修复方向**：账号锁独立于 IP 生效（`blocked_until` 命中即拒），IP 锁作为第二道；或者把 `_portal_user_blocked` 的键改成仅 `username`。

---

## D-3【中】登录入口是**全局 4 并发名额**，无按来源分配，可定向占满导致管理员无法登录

- **位置**：`server/routers/auth.py:16-17`（`_login_active = 0`、`_LOGIN_MAX_ACTIVE = 4`）、`server/routers/auth.py:59-69`；门户同构 `server/routers/portal.py:22`、`:155-158`。
- **现象**：`_login_active` 是模块级全局计数，`if _login_active >= 4: 429`。四个并发登录请求（哪怕来自四个不同 IP）就把所有真人挡在门外。登录路径本身要跑 PBKDF2（`server/security.py:26` `iterations=260000`），单次占用几十毫秒到上百毫秒。
- **为什么是问题**：这是**可用性**问题，不是数据泄露。攻击者只需维持 4 个并发登录请求（不需要正确密码，甚至不需要合法用户名——`dummy_kdf` 在用户不存在时也会跑，`server/routers/auth.py:79`），就能让管理员持续收到 429，无法在事故中登录处置。与 D-1 叠加后成本极低：D-1 保证撞库请求不被锁，D-3 保证登录入口被占满。
- **怎么验**：`server/tests/test_login_admission.py` 已覆盖名额逻辑（`auth._login_active = 4` 时拒绝）。动态复现：4 个并发 `POST /api/login`（错误密码）后，第 5 个正确密码请求返回 429。
- **修复方向**：名额按来源 IP 分组（每 IP 1–2 个），或给 `admin` 账号保留一个专用名额；至少不要把真人登录与机器撞库放在同一个全局池里。

---

## D-4【中】CSRF 同源校验以请求 `Host` 为锚点，DNS rebinding 可绕过（已实测）

- **位置**：`server/security.py:1055-1067`（`expected_origin` 用 `request.headers['host']` 拼 triple）、`server/security.py:1086-1135`（`check_same_origin`）、调用点 `server/security.py:927-950`（`_current_user` 内）。
- **现象**：`expected_origin` 的唯一来源是请求自带的 `Host` 头：
  ```python
  # server/security.py:1057
  host = request.headers.get('host')
  ```
  只要攻击者能让浏览器把 `Host` 变成自己的域名（DNS rebinding 即可），`Origin` 与 `Host` 就同源，校验通过。实测（`TestClient`，已登录 admin Cookie）：
  ```
  POST /api/tokens  Host: evil.test  Origin: http://evil.test   -> 200（创建成功）
  POST /api/tokens  Host: evil.test  Origin: http://other.test  -> 403（CSRF）
  ```
  第一条即为绕过：同源判定只比对了 `evil.test` 自己。
- **为什么是问题**：管理面全部写端点依赖这一处校验（`check_same_origin` 在 `_current_user` 里统一挂载，`server/security.py:950`）。DNS rebinding 在「管理面暴露在可被解析的域名上」时是现实攻击（不需要用户点链接之外的条件）。绕过成功后，攻击者可在受害者浏览器里以管理员身份创建 API Token、改设置、导出密钥。
- **怎么验**：上面的两条 `TestClient` 请求即为最小复现（脚本见「复现脚本」节）。生产环境等价验证：用 `curl --resolve` 把自有域名解析到面板 IP，带管理员 Cookie 发 `POST /api/tokens`，观察是否 200。
- **修复方向**：`expected_origin` 改为**配置白名单**（`WB_ALLOWED_ORIGINS`），不信任请求头；或在 `check_same_origin` 里增加「`Host` 必须属于配置的域名列表」的前置检查。至少对 `POST/PUT/DELETE` 强制 `Sec-Fetch-Site: same-origin`（当前只把 `Sec-Fetch-*` 当「伪造迹象」的辅助判据，`server/security.py:1097`）。

---

## D-5【中低】管理端会话 Cookie 是 `SameSite=Lax`，且无 `__Host-` 前缀

- **位置**：`server/routers/auth.py:136`（登录签发）、`server/main.py:872`（滑动续期）；对照门户 Cookie 是 `strict`（`server/routers/portal.py:200`、`:211`）。
- **现象**：管理端 `wb_session` 用 `samesite='lax'`。Lax 允许**顶层 GET 导航**携带 Cookie。配合 `check_same_origin` 对 GET 直接放行（`server/security.py:1109-1110`），任何被误实现为 GET 的写操作都会变成 CSRF 入口。
- **为什么是问题**：目前全仓 `@router.get` 处理函数里没有发现写库操作（脚本扫描 `server/routers/*.py` 中带 `db.execute`/`audit(` 的 GET 处理函数，仅命中 `server/routers/accounts.py:114` 的 `export_accounts`，它只写审计、不写业务数据）。所以**当前没有可利用的 GET 写**，但这是「靠约定维持」的安全边界：任何人将来加一个 GET 写端点，CSRF 立刻成立。门户侧已经用 `strict`，管理端没有理由更松。
- **怎么验**：`grep -n "samesite" server/routers/auth.py server/main.py server/routers/portal.py` → `lax` / `strict` 对照。
- **修复方向**：管理端改为 `samesite='strict'`（或 `lax` + 全量 GET 写扫描进 CI）；Cookie 名加 `__Host-` 前缀（要求 `Secure`、`Path=/`、无 `Domain`），可挡子域注入。

---

## D-6【中低】`account_admin` 的读白名单包含 `/api/tokens`，普通管理员能看到全部机器凭据元数据

- **位置**：`server/userroles.py:30-49`（`PANEL_READ_ROUTES` 含 `'/api/tokens'`）、`server/routers/tokens.py:...`（`GET /api/tokens` 用 `require_session_reader`）。
- **现象**：实测 `account_admin` 会话 `GET /api/tokens` → 200，返回每把 Token 的 `name`、`scope`、`created_by`、`last_used_at`、`last_used_ip`（`server/tokensvc.py:76` 附近 `_parse`）。
- **为什么是问题**：`account_admin` 的职责是「加账号」（`server/userroles.py:1` 注释「普通管理员」），但它能枚举机器凭据的**存在性、作用域、最近使用 IP**。这本身不能直接登录（Token 明文只在创建时返回一次），但它是横向移动的侦察面：知道哪些 Token 是 `admin` scope、从哪个 IP 用过。结合 `/api/users`（`server/routers/settings.py:313`）能拿到管理员用户名，攻击面被显著放大。
- **怎么验**：实测（`account_admin` 会话）`GET /api/tokens` → 200。
- **修复方向**：把 `'/api/tokens'` 从 `PANEL_READ_ROUTES` 移除，或对 `account_admin` 只返回 `name`+`scope`（去掉 `last_used_ip`/`created_by`）。

---

## D-7【低】登录失败计数内存与 DB 双写，DB 写入失败被静默吞掉

- **位置**：`server/security.py:596-611`（`_mirror_fail` 的 `except Exception: pass`）、`server/security.py:614-620`（`_unmirror_fail`）。
- **现象**：每次失败同时写内存与 SQLite（`login_fails_ip` / `login_fails_user`），写库失败时完全静默。单 worker 部署（`deploy/...` 里 `uvicorn ... --workers 1`）下内存是权威，重启后从 DB 重载（`server/security.py:572` `_load_fails_once`）。如果 DB 写入持续失败（磁盘满、锁冲突），重启后所有锁定状态丢失，攻击者只要等到一次重启就能重置计数。
- **为什么是问题**：锁定状态是**安全状态**，丢失等于给攻击者一次免费重置。静默失败意味着运维不会知道。
- **怎么验**：`grep -n "except Exception" -A2 server/security.py | sed -n '1,40p'`，可见 `_mirror_fail` 与 `_unmirror_fail` 都吞异常。
- **修复方向**：失败时至少 `logger.warning` 并计数（暴露到 `/api/security/config` 或指标）；或在 DB 不可写时降级为「拒绝登录」而不是「继续但只记内存」。

---

## D-8【低】网关密钥无盐 SHA-256 存储

- **位置**：`server/keysvc.py:53-54`（`def _hash(...): return hashlib.sha256(...).hexdigest()`）。
- **现象**：`api_keys.key_hash` 存无盐 SHA-256。密钥是 128 位随机（`server/keysvc.py:...` 生成 32 hex），离线爆破不可行，所以风险有限；但无盐意味着**相同密钥产生相同哈希**，且一旦 DB 泄露，攻击者可以对照已知密钥（比如从日志/配置里拿到的同一把）确认命中，无需爆破。
- **为什么是问题**：低。真正的风险是「DB 泄露 + 密钥熵不足」的组合，而当前熵足够。列为低，不建议优先修。
- **怎么验**：`grep -n "sha256" server/keysvc.py`。
- **修复方向**：改用 HMAC-SHA256（pepper 存环境变量）或 Argon2；迁移期双读。

---

## D-9【低】`/api/release` 未鉴权，泄露发布版本与 commit

- **位置**：`server/main.py:579-589`（`@app.get('/api/release', include_in_schema=False)`，无 `Depends`）。
- **现象**：匿名 `GET /api/release` 返回 `release`、`commit`、`build_time`。实测匿名 200。
- **为什么是问题**：低。信息泄露类：攻击者可据此定位已知漏洞版本（配合 CHANGELOG）。不是凭据泄露。
- **怎么验**：匿名 `GET /api/release` → 200 + JSON。
- **修复方向**：加 `Depends(security.require_panel_reader)`，或只返回 `release` 不返回 commit。

---

## D-10【信息】qtgcore 兼容层路由自身无 `Depends`，鉴权完全依赖 `main.py` 中间件

- **位置**：`server/qtgcore/routers/*.py`（`grep -rc "Depends" server/qtgcore` 全为 0）；鉴权在 `server/main.py:715-819` 的 `_qtg_platform_route` 中间件里（`server/main.py:769` 调 `security.current_user`）。
- **现象**：22 个兼容层端点（`/api/logs`、`/api/accounts`、`/api/keys` 等）在路由层没有鉴权依赖，靠中间件统一拦截。**实测匿名请求被正确拦下**（`GET /api/logs` 无 Cookie → 401 `auth required`），所以**当前没有漏洞**。
- **为什么记下来**：这是结构性风险——任何人新增一个兼容层路由，只要忘了走中间件（比如直接 `app.include_router` 而不经过 `_qtg_should_route`），就会变成匿名端点。建议在 CI 里加一条「所有 `/api/*` 路由必须有 `Depends` 或出现在 `_qtg_should_route` 白名单」的断言。
- **怎么验**：`grep -rn "Depends" server/qtgcore/routers | wc -l` → 0；匿名 `GET /api/logs` → 401。

---

## 已验证、未发现问题的面（供合并去重）

| 面 | 结论 | 证据 |
|---|---|---|
| SQL 注入 | 无。用户输入全部走参数绑定；f-string 只拼表名/列名，且来源是常量或 `sqlite_master` | `grep -rn "execute(f" server --include=*.py` 逐条核对：`server/archival.py:298/322`、`server/retention.py:484`、`server/keysvc.py:357`、`server/security.py:609/619` 均为常量 |
| 命令注入 | 无。`subprocess.run` 全部传 list（无 `shell=True`） | `grep -rn "shell=True" server` → 0；`server/backup_application.py:107`、`server/services/keyimport.py:189` 等均为 list |
| 路径穿越（上传/导出） | 无。账号上传文件名只用于展示，落盘用 uid；备份解包有成员名校验 | `server/routers/accounts.py:188-270`（`upload_accounts`）、`server/routers/accounts.py:173`（`_persist_uploaded_account`）、`server/backup_application.py:188`（`if '..' in parts`） |
| SSRF | 有防护。`_reject_internal_host` 拦回环/链路本地/元数据域名 | `server/services/wb2api.py:2125`、`server/tests/test_ssrf_guard.py` 通过 |
| YAML 加载 | 无。全仓不使用 `yaml` | `grep -rn "yaml" server --include=*.py` → 0 |
| 备份里的明文凭据 | **本地归档是明文**：`recovery-<ts>.tar.gz` 含 `manager.db`、`users.json`、上游 auth 与主机元数据，`backup_application.py:435–524` 不加密；只有异地发布 `offsitebackup.py:122-129` 用 `openssl cms -encrypt` 写密文。两者必须区分（`docs/DISASTER-RECOVERY.md` 已区分） | `sed -n '430,530p' server/backup_application.py`；`grep -n 'encrypt' server/offsitebackup.py` |
| 上游凭据掩码 | 已掩码。`/api/settings/upstream` 返回 `device_token` 等敏感字段为 `***` | `server/services/wb2api.py:1593`（`load_upstream_config` docstring）、`server/routers/settings.py:41-43` |
| 网关密钥明文 | 只在创建时返回一次；`GET /api/keys` 不返回明文 | `server/keysvc.py:...`（`create` 返回 `secret`，`list` 不返回） |
| 抓包脱敏 | `REDACT_KEYS` 覆盖 `authorization`/`device_token`/`cookie`/`api_key` 等；`_scrub` 在写入前统一执行 | `server/traffic.py:50-54`、`:164`、`:727` |
| 会话版本绑定（DAN-11） | 已实现。Cookie 载荷含 `sv`，`_cookie_identity` 校验 `sv` 与 `orig`/`iat`/`exp` | `server/security.py:828-890`、`server/security.py:422-453`（`issue_token`） |
| 会话绝对寿命 | 有。`exp = orig + SESSION_DAYS*86400`，续期不延长 `orig` | `server/security.py:451`、`server/config.py:277`（`SESSION_DAYS=1`） |
| 密钥轮换过渡窗口 | 有。`secret_previous` 双签 | `server/security.py:...`（`_cookie_identity` 内 `_unsign(..., secret_previous)`） |
| 登录限流（IP） | 有。`MAX_FAILS=5/600s`，内存 + DB 双写 | `server/security.py:554-556`、`:666-679` |
| 密钥 IP 绑定 / `max_ips` | 有。请求路径上校验 | `server/keysvc.py:577`（`validate`）、`:658`（`ip_allowlist`）、`:665`（`max_ips`）、`:720`（`claim_ip`） |
| 请求体上限 | 有。`/api/*` 16 MiB，登录 64 KiB | `server/main.py:491`（`MAX_API_BODY_BYTES`）、`:515`（`limit_api_body`）、`server/main.py:...`（`LOGIN_BODY_LIMIT`） |
| CORS 通配 | 有防护。`*` + credentials 直接拒绝启动 | `server/main.py:...`（`if '*' in CORS_ORIGINS: raise RuntimeError`） |

---

## 跑过的安全测试与结果

```bash
cd /d/AI/capy/worktrees/jam_01M8EY6H3B8VWJYY4C33BWWNHD/zhihui-gateway
PYTHONUTF8=1 WB_DATA_DIR=/tmp/audit-d/data \
  "D:/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe" -m pytest \
  server/tests/test_audit_20261029_security.py \
  server/tests/test_security_audit_hardening.py \
  server/tests/test_ssrf_guard.py \
  server/tests/test_auth_bypass.py \
  server/tests/test_api_tokens.py \
  server/tests/test_security_lockout.py \
  server/tests/test_audit_auth_origin.py \
  server/tests/test_login_admission.py \
  -q -p no:cacheprovider
```

**结果**：`116 passed, 2 warnings, 46 subtests passed`（约 118 秒）。含 `test_security_audit_hardening.py` 的 18 个用例、`test_api_tokens.py` 的权限矩阵断言、`test_ssrf_guard.py`、`test_audit_auth_origin.py`。

> 注意：`test_security_audit_hardening.py:206` 在**未设 `PYTHONUTF8=1`** 的 GBK 控制台下会因 `UnicodeDecodeError: 'gbk' codec` 失败（测试读 gzip 文本时用了默认编码）。设 `PYTHONUTF8=1` 即通过。这是**测试环境编码问题**，不是产品缺陷，但会让中文 Windows 上的 CI 误报，建议测试里显式 `encoding='utf-8'`。

---

## 复现脚本（D-1 / D-4）

```bash
cd /d/AI/capy/worktrees/jam_01M8EY6H3B8VWJYY4C33BWWNHD/zhihui-gateway
PYTHONUTF8=1 WB_DATA_DIR=/tmp/audit-d/data \
  "D:/AI/capy/work/venvs/zhihui-gateway/Scripts/python.exe" - <<'PY'
import sys, pathlib; sys.path.insert(0,'.')
from fastapi.testclient import TestClient
from server.main import app

# --- D-1：账号锁被 IP 架空 ---
from server import security
for i in range(1,7):
    ip=f'203.0.113.{i}'
    print('D-1', ip, 'blocked_before=', security.login_blocked(ip,'admin'))
    security.record_fail(ip,'admin')

# --- D-4：Origin 以 Host 为锚 ---
c=TestClient(app)
pw=pathlib.Path('/tmp/audit-d/data/initial-admin-password.txt').read_text().strip().split(':')[-1]
c.post('/api/login', json={'username':'admin','password':pw},
       headers={'Host':'testserver','Origin':'http://testserver'})
for host,origin in (('evil.test','http://evil.test'),('evil.test','http://other.test')):
    r=c.post('/api/tokens', json={'name':'probe'},
             headers={'Host':host,'Origin':origin})
    print('D-4', host, origin, '->', r.status_code)
PY
```

预期：D-1 六个 IP 全 `blocked_before=False`；D-4 第一行 `200`（绕过）、第二行 `403`。

> 复现注意：`TestClient` 不走 lifespan，因此 `credit_watch` 等后台任务会因缺少 `platform_credit_snapshots` 表打印 `OperationalError`——**这是测试环境现象，不是产品缺陷**（生产由 lifespan 建表）。不影响上面的结论。
