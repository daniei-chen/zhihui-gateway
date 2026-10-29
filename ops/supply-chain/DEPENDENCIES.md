# K49 依赖与构建供应链（候选）

## 现状（审计材料 + 本次可见证据）

- 前端 `package-lock.json` 存在（`src/three-platform/web/package-lock.json`，319 KB），Next 15.5.25、React 19；
- 既有 `npm audit` 告警涉及 Next/sharp/follow-redirects/source-map-js；静态导出让其中一部分不可达，
  但"版本新"不代表"无漏洞"；
- Manager 使用 `requirements.txt` + `requirements.lock.txt` + `requirements.hashes.txt`（有哈希锁）；
- 容器：WorkBuddy 镜像 `workbuddy2api-upgrade:20261022.4`（tag），Qoder 镜像已 digest 固定，
  Trae `ghcr.io/smart-open/traeworkassistant:1.0.0`（tag）。

## 候选做法

1. **锁定并记录**：前端用 `npm ci`（不是 install）；Manager 用 `pip install --require-hashes`；
2. **SBOM**：对前端与 Python 依赖各生成一份（`cyclonedx` 格式即可），随 release 归档；
3. **漏洞复核**：对每条 audit 告警写"是否可达"的结论（静态资源不经过 Next 服务端 → 相应 RCE 类不可达）；
4. **升级纪律**：不在生产 venv 原位升级；在独立构建环境升级 → 跑测试 → 再发布；
5. **镜像绑定**：所有容器固定 digest，并把 digest 与源码 commit 写进发布清单；
6. **依赖变更门禁**：lockfile 变化必须进 release 记录（谁、为什么、影响哪些包）。

## 工具（本地可跑）

```bash
# 前端：锁文件一致性
cd src/three-platform/web && npm ci --ignore-scripts --dry-run
# 依赖清单（生成 SBOM 的最小替代：直接冻结 node_modules 树）
npm ls --all --json > /tmp/web-deps.json

# Python：哈希一致性（需要网络时在隔离环境）
python -m pip install --dry-run --require-hashes -r manager/server/requirements.txt
```

## 未验证

- 未连接 npm/pypi 网络，未实际跑 audit；
- 未确认生产 venv 与 lock 文件一致（需要服务器只读对比）；
- 未验证容器 digest 与源码 commit 的对应关系（需要镜像仓库元数据）。
