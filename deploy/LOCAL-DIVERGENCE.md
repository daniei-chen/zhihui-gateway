# deploy/ 本地差异与决策记录

> 2026-10-04 首次审阅（管理端 v1.0.71 → v1.0.79 升级后）。
> 背景：一键更新**故意跳过 `deploy/` 目录**（它是信任锚，不能随发布包替换），
> 因此每次升级都会列出「待人工确认」的差异。这份文件记录**已确认过的结论**，
> 下次升级只需 diff 这些文件、对照本文件即可，不必重新推演。

## 一、信任锚（每次升级都必须复核，且必须一致）

| 文件 | 2026-10-04 结论 |
|---|---|
| `release-signing-key.pub` | **与发布包完全一致**（sha256 `85250f750fa7b1aa…`），密钥未轮换 ✓ |
| `verify-release.sh` | **0 行差异** ✓ |
| `purge_credentials.py` | **0 行差异** ✓ |
| 独立复核 | 用本机公钥重新验签 v1.0.79 包 → `Good "file" signature … ED25519` ✓ |

## 二、`update.py`：**保留本地版本，不要用发布包覆盖** ⚠️

本地版有两处**必要的本地适配**，发布包版本**没有**：

1. `UPSTREAM_REPO` 默认值 = `https://github.com/daniei-chen/WorkBuddy-API.git`
   （发布包仍是 `Sliverkiss/workbuddy2api.git` —— 该仓库 **2026-09-23 起 404**）
2. `apply_source_patches()`：构建容器前重施 `UPSTREAM_DIR/local-patches/patches/`，
   且 **fail-closed**（补丁打不上就中止更新、保留旧容器，不静默降级）

发布包版本相对本地版多出来的内容：主要为 **Windows 原生部署**支持与文案调整；
经复核**没有安全回退**——验签仍在解压之前执行（`verify_release_signature` →
`tarfile.open`），唯一跳过开关仍是原有的 `WB_SKIP_SIGNATURE=1`（带显式告警）。

**维护建议**：若将来想吸收上游 update.py 的改进，做法是**手工合并**（把上面两点
移植到新版），而不是直接覆盖。复核命令：

```bash
tar xzf workbuddy-manager-<版本>.tar.gz -C /tmp/
diff /opt/workbuddy-manager/deploy/update.py /tmp/workbuddy-manager-<版本>/deploy/update.py | grep -E '^[<>]'
```

## 三、其余文件的结论

| 文件 | 差异 | 决策 |
|---|---|---|
| `workbuddy-web.service` | 仅 `Description=` 一行（文案）| **不采纳**：已安装单元在 `/etc/systemd/system/`，且本地描述更准确；本目录副本只是 install.sh 的模板 |
| `README.md` | 264 行（文档，7.9KB → 18.8KB）| **已采纳**（纯文档，含 fork-image / windows-native 说明）|
| `install.sh` | 75 行 | **不采纳**：本地版已把 `UPSTREAM_REPO` 指向自家 fork；发布包版教用户用环境变量覆盖，对全新安装有效，对本机无意义 |
| `check-upstream.sh`（新增）| —— | **已采纳**：发版前检查上游是否有新提交的维护者工具，与运行无关但有用 |
| `fork-image/`、`windows-native/`（新增）| —— | **不采纳**：与本机（Linux 容器部署）无关，其说明已包含在 README 中 |

## 四、下次升级的复核清单（照抄即可）

```bash
# 1) 信任锚是否被轮换（必须一致，否则停下来人工判断）
sha256sum /opt/workbuddy-manager/deploy/release-signing-key.pub
diff /opt/workbuddy-manager/deploy/verify-release.sh <新包>/deploy/verify-release.sh

# 2) update.py 是否出现值得手工合并的改动
diff /opt/workbuddy-manager/deploy/update.py <新包>/deploy/update.py | grep -E '^[<>]'

# 3) 本地两条适配是否还在（少一条都会出问题）
grep -n "UPSTREAM_REPO = os.environ" /opt/workbuddy-manager/deploy/update.py
grep -n "def apply_source_patches"   /opt/workbuddy-manager/deploy/update.py
```
