# K50 容器加固与镜像来源（候选）

## 现状（2026-10-23 只读）

| 容器 | 镜像 | 状态 | 已知属性 |
|---|---|---|---|
| `aiwork-server`（Trae） | `ghcr.io/smart-open/traeworkassistant:1.0.0` | healthy | tag 部署，非 digest |
| `workbuddy2api` | `workbuddy2api-upgrade:20261022.4` | healthy | tag 部署，非 root（app/UID 10001），1.5GB 内存限制 |
| `qoder-proxy` | `16e86bb080b0` | healthy | digest 固定 |

## 候选加固（逐项独立验证，不一次全开）

```bash
# 1) 只读根文件系统 + tmpfs 可写目录（先 dry-run 观察是否需要写）
docker run --read-only --tmpfs /tmp:rw,size=64m ...

# 2) 能力裁剪（大多数服务不需要任何 capability）
--cap-drop=ALL --security-opt=no-new-privileges

# 3) 进程数上限（防 fork 炸弹）
--pids-limit=256

# 4) 资源上限（与 K19 的整机预算一起算）
--memory=1g --memory-swap=1g --cpus=1

# 5) 镜像来源绑定
docker pull <img>@sha256:...   # 部署时用 digest，不用 tag
```

## 镜像来源要求

1. 每个容器必须能回答：digest、构建 commit、构建时间、Dockerfile 位置；
2. 不允许 `latest`；tag 只作人类可读别名，部署用 digest；
3. 镜像内的二进制与源码对应关系要能证明（WorkBuddy 是 Go 容器，需要 build info）。

## 验证清单

- `docker inspect --format '{{.Config.Image}} {{.Image}}'` 记录镜像与 digest；
- `docker run --rm <img> id` 确认运行用户；
- 加 `--read-only` 后跑一次冒烟（登录/推理/取消），确认没有隐藏写路径；
- 回退：去掉 `--read-only`/`--cap-drop` 重新起（单独回退，不整体回滚）。

## 未验证

- 容器内实际写路径（需要 `docker diff` 或审计日志）；
- Trae 与 Qoder 镜像的源码对应关系；
- 只读根文件系统对服务的影响（必须逐容器验证，不能"看起来能跑"就上）。
