# K17 凭据清单与轮换 runbook（未部署）

**原则**：先盘点（用途/指纹/访问者），再平滑轮换（双接受窗口），最后撤销旧值。
不把值写进任何报告、不把私钥拷到生产、不以改扩展名代替保密。

## 已知凭据（只登记指纹与位置，不含值）

| # | 名称 | 位置 | 权限 | 指纹/状态 | 轮换方式 |
|---|---|---|---|---|---|
| 1 | QTG 内部 Bearer（Nginx 注入） | `/etc/nginx/secrets/qtg_bearer.conf` | 600 root | sha256 `036f432a…`，350B，2026-10-23 18:34 | Nginx 与 QTG 同时改（双接受） |
| 2 | Manager 会话签名密钥 | `/opt/workbuddy-manager/data/users.json` 内 secret | 600 | 未读取 | 改密后所有会话失效，需公告 |
| 3 | Qoder 面板密码 | `/opt/qoder-hub/secrets/panel_password` | 600 root | 未读取 | Qoder 侧改 + QTG 配置更新 |
| 4 | Manager 管理 Token（wbt_） | DB `api_tokens`（只存哈希） | — | 未列举 | UI 吊销重建 |
| 5 | 网关推理密钥（wbk_） | DB `api_keys`（只存哈希） | — | 未列举 | 用户自助轮换 |
| 6 | 备份加密公钥 | `/root/backups/workbuddy-recovery/recovery-public.pem` | 644 | 已登记 | 私钥离线，公钥可换 |
| 7 | 异地 push 凭据 | `offsite.json`（target/branch 两个键） | 600 | 未读取 | 按 git 密钥流程 |
| 8 | 原审计正文中的凭据 | 审计材料 | — | 已扩散 | 必须视为已泄露，优先轮换 |

## 轮换 runbook（以 #1 为例）

1. 记录当前指纹与引用者：`grep -rl qtg_bearer /etc/nginx`（只读）；
2. 生成新值，先让**接收方**接受新旧两个值（如需双接受窗口）；
3. 更新发送方（Nginx include），`nginx -t` + `reload`；
4. 观察 QTG 调用无 401；
5. 撤销旧值，记录时间与操作者；
6. 回退边界：不要为了回退恢复已撤销的旧凭据；不在 `.pre` 文件里留明文。

## 备份与分发

- 含凭据的备份必须加密（`recovery-public.pem` 已是 age 公钥）；
- 本地明文归档（`/root/backups/workbuddy-daily`）已含 WorkBuddy 账号池（A2 校验证实 40 个 auths 文件），
  单盘单机 = 明文风险；至少做到：目录 700、属主 root、不出现在普通用户可读路径；
- 新报告与证据一律不含明文；现有证据文件已按此处理。

## 未验证

- 各凭据的真实消费者清单（需要只读 grep 全量配置与进程环境）；
- 旧值是否仍然有效（"旧文件存在"不等于"旧值可用"）；
- 原审计材料中凭据是否已被撤销（需要人工确认）。
