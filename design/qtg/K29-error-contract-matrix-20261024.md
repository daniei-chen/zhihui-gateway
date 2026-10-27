# K29 错误契约矩阵（2026-10-24 接管实测）

生产未连接；fixture bd6d26da153a（源 09ffa9e2）。证据：
- `evidence/k29-error-contract-2026-10-24-ba3f20/matrix.json`（480 探针：~102 客户端契约端点 × workbuddy/qoder 路由 × 匿名/只读/管理员）
- `evidence/k29-csrf-recheck.json`（带 Origin 复测 20 探针）
- 探针：`tests/linux_lab/probe_error_contract.mjs`（动作类端点绝不携带管理员凭证；写端点管理员仅空 body 验证探测）

## 信封类别（实测归类）

| 状态 | 形状 | 出现处 | 定性 |
|---|---|---|---|
| 401 | **HTML**（nginx auth_request 拦截，170/172） | 所有匿名 /api/*（除 /api/me） | API 客户端拿到 HTML 而非 JSON——K29 须写入契约：401 靠状态码，不靠 body |
| 401 | `{detail:str}` | 仅 `/api/me`（应用层返回） | 两层 401 形状并存 |
| 403 | `{detail,code,reason}` | QTG 写操作缺 Origin（CSRF 拒绝，50 例） | K07 CSRF 契约在写面全量生效；带 Origin 后全部转真实响应 |
| 403 | `{detail:str}` | Manager 角色不足（viewer 写/敏感读，48 例） | 一致 |
| 404 | `{detail:str}` | Manager 不存在资源（密钥/用户/规则/未知路径） | 一致 |
| 405 | `{detail:str}` | Manager GET /api/keys/{id}（无此路由） | 方法契约 |
| 409 | `{detail:str}`（"本部署不支持…"可操作文案） | qoder/trae 的 tokens/users/proxy-routes | **平台能力声明信封**（K29 每能力标平台的第一手数据） |
| 410 | `{available:false,code,detail}` | qoder/trae 红包 | 平台不实现（营销功能仅 WorkBuddy） |
| 422 | `{detail:[obj]}`（FastAPI 校验数组） | Manager 校验失败 | errText 已按 N14 兜底 |
| 501 | `{available:false,code:not_implemented,detail}` | qoder/trae 未知路径 | "未实现"≠"没有数据"的有意区分（N20） |
| 502 | `{error:{code,message,type}}` | 引擎控制面不可用（fixture 预期） | N14 信封 |

## 新发现（本批）

1. **DELETE 语义跨平台不一致**：qoder `DELETE /api/keys/999999` → **200 ok**（不存在当成功）；Manager → 404"密钥不存在"；同平台 PATCH 同 ID → 404。幂等删除 vs 显式 404 需在 K29 契约表定裁并统一或文档化。
2. **QTG 空 body 创建密钥成功**：qoder `POST /api/keys` + `{}` → 200，创建名为"未命名"的密钥（无名称校验）；Manager 同输入 → 422。最小必填字段跨平台不一致。（探针误建已清理，evidence 清理记录在矩阵运行日志。）
3. `GET /api/keys/import-local/detect` 管理员也 403（非本机调用者）——与 PB0052 一致，属设计。
4. viewer 对不存在资源 GET 得 404（先存在性后角色）——常见取舍，记录不改。

## 对 K29 收敛的输入

逐端点核对已有机器矩阵基线；剩余工作：把上表写入正式契约文档、逐能力（K29 任务卡）标平台/角色/错误码/可重试、决定 DELETE 语义与空 body 最小字段、401-HTML 是否文档化或改 JSON。
