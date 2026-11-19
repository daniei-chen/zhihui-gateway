# v1.3.4 国内服务器替换记录（2026-11-19）

> 目标：把「豆包工作取代 qoder」的改造从本地推到国内服务器，**不影响 workbuddy**。
> 本文只记录**实际做过并被验证**的事。没验证的不写。

## 一、结论

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 后端 release | `v1.3.4`，`current` 已切换 | `readlink -f .../releases/current` → `v1.3.4` |
| 三平台枚举 | `doubaowork` / `trae` / `workbuddy` | 启动日志 `qoder=off(-) trae=trae2 wb=workbuddy dbw=doubaowork` |
| qoder 摘除 | 引擎不创建（`off`） | 同上，`qoder=off(-)` |
| workbuddy | **未改动**，持续出流量 | 10 分钟内 26 次 `chat/completions 200` |
| 前端 | 已换成三平台 UI | 线上 chunk `label:"Qoder"` = 0，`doubaowork` = 8 |
| 服务 | `active`，7866 监听 | `systemctl is-active` / `ss -lntp` |

## 二、后端部署链

```
build-release.sh v1.3.4 5dafe12 <v1.3.3> <manifest> <cache>
  → cp -a v1.3.3 v1.3.4      # 继承 web/out 与未变更文件
  → 覆盖 manifest 里的 28 个 server/ 文件（三道校验：存在/形状/哈希）
create-managed-release.py v1.3.4     # 724 个文件的 candidate_sha256
openssl pkeyutl -sign -inkey release-priv.pem   # Ed25519
ln -sfn v1.3.4 current && systemctl restart workbuddy-web
```

清单哈希取自 **git blob**（`git cat-file blob HEAD:<path>`），不是工作区文件 ——
本地 `core.autocrlf=true` 会把磁盘文件变成 CRLF，哈希必然对不上。

## 三、两个踩过的坑（都会让服务起不来）

### 1. `writer-capabilities.json` 的 `release_id` 必须等于目录名

`state_guard.inspect_release` 里有一条：

```python
value['release_id'] != root.name → Rejected('unsupported writer descriptor')
```

`build-release.sh` 只覆盖 manifest 里列出的文件，而
`server/writer-capabilities.json` **不在我的 diff 里**，于是它从 v1.3.3
原样继承过来，内容仍是 `"release_id": "v1.3.3"`，而目录名是 `v1.3.4` ——
准入直接拒绝，服务反复重启到 `Start request repeated too quickly`。

**修法**：改 `release_id` 为目录名，然后**重新生成并重新签名** manifest
（这个文件在签名闭包里，改完不重签会被 `verify_release` 拦下）。

### 2. 不要在 `writer-guard` 目录里跑没加 `-B` 的 Python

`verify_host_closure()` 会比对 `/opt/zhihui-release/writer-guard/` 的
**目录条目集合**与 `host-files.json`。用 `python3` import 该目录下的模块
会生成 `__pycache__`，集合多一项 → `Rejected('installed host closure differs')`。

这个坑的杀伤面很大：**它会让 v1.3.3 也起不来**，看起来像"新版本把服务搞坏了"，
实际是排查动作自己造成的。诊断脚本一律加 `-B`，或跑完立刻
`rm -rf /opt/zhihui-release/writer-guard/__pycache__`。

## 四、前端部署（两个位置，缺一不可）

nginx 把前端拆成两处，只换 release 里的 `web/out` **不会生效**：

| 内容 | 实际来源 |
| --- | --- |
| HTML 页面 | `location ~ ^/(dashboard\|accounts\|...)$` → `root /var/www/zhihui-console-current` |
| `/_next/` | `alias /var/www/zhihui-next-pool/` |

所以步骤是：

1. 在服务器上用源码构建（`npm install` + `npm run build:export`，Node v22.23.3）
2. 替换 `releases/v1.3.4/web/out`，用 `create-web-manifest.py` 重签（257 个文件）
3. 重新生成 release 的 `managed-release.json` 并重签（`web/out` 在闭包内）
4. `cp -a web/out /var/www/zhihui-console-releases/<buildId>`
5. `cp -a web/out/_next/static/. /var/www/zhihui-next-pool/static/`
6. `ln -sfn .../zhihui-console-releases/<buildId> /var/www/zhihui-console-current`
7. 更新 `/opt/workbuddy-manager/web/current.json` 的 `buildId` 与 `supportedBuilds`
8. `nginx -t && systemctl reload nginx`

本次 `buildId = S3o_7fa0c5r6GH0UplWJy`。

**域名别搞错**：智汇网关是 `zhihui.kaogong.art`；
`deepseek.kaogong.art` 是另一个站点（DSH），拿它测 `/_next/` 只会得到
SPA 回退的 HTML，看起来像"资源 404"，实际是测错了站。

## 五、前端源码修复

`components/common/layout/PlatformToggle.tsx` 的 `options` 是**硬编码**的，
`lib/platform-store.ts` 里的 `PLATFORMS` 改了它不会跟着变 —— 这是本次
UI 仍显示四个平台的直接原因。已改为三平台，顺序：豆包工作 → Trae → WorkBuddy。

`RealmToggle.tsx` 不需要改：它已经有 `noRealm` 分支，豆包工作下版本切换器
显示"此切换对它不生效"。

`Platform` 类型仍保留 `'qoder'`，这是刻意的：旧密钥数据里可能还有
`platform: 'qoder'`，删掉类型会让 `.map` 崩掉。

## 六、回滚

```bash
# 后端
ln -sfn /opt/workbuddy-manager/releases/v1.3.3 /opt/workbuddy-manager/releases/current
systemctl restart workbuddy-web

# 前端
ln -sfn /var/www/zhihui-console-releases/hYnoApn9elRusIBfLESTS /var/www/zhihui-console-current
# 并把 current.json 的 buildId 改回 hYnoApn9elRusIBfLESTS
systemctl reload nginx
```

保留的资产：

- `/opt/workbuddy-manager/releases/v1.3.3`（完整上一版）
- `/root/qtg-deploy/web-out-backup`（v1.3.4 换前端前的 out）
- `/root/qtg-deploy/qoder_engine.bak`（原 `qoder_engine` = `qoder2`）
- `/root/qtg-deploy/manifest.txt`（28 文件哈希清单）

## 七、未做/未验证

- **豆包上游 `doubaowork2api` 不在这台机器上**：18789 未监听，
  `/opt/doubaowork2api` 不存在。引擎已开（`var/doubaowork_engine = on`），
  适配器对上游不可达是安全容错（`health` 返回 `up: false`，不崩），
  但**豆包平台此刻没有真实账号可用**，要等上游部署。
- `qoder2-engine.service` 仍在跑（端口 8799）。qtg 已不连它
  （`var/qoder_engine = off`），停不停是运维决定，本次没动。
- 前端 `_next` 资源池里历届 buildId 并存（本次新增一个，未清理旧的）。
