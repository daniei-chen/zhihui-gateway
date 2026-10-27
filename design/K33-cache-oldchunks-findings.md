# K33 缓存与旧版静态资源兼容 · 实测结论与修复方案

- 状态：**已修复并实测通过**（2026-10-24，方案 A）
- 修复后证据：`evidence/k33-cache-oldchunks-FIXED-20261024.json`（6/6 检查通过）
- 证据：`evidence/k33-cache-oldchunks-20261024.json`（真实 Nginx，两代真实构建 A→B）
- 演练脚本：`tests/linux_lab/verify_k33_cache_oldchunks.py`

## 1. 实测结果（真实 Nginx + 真实两代构建）

| 检查项 | 结果 | 实测值 |
|---|---|---|
| HTML 切换后可用 | ✅ | 200 |
| HTML 不被长期缓存 | ✅ | `Cache-Control: no-cache, must-revalidate` |
| **旧 chunk 切换后可达** | ❌ | **3/5 可达**；2 个 404 |
| hash 资源 immutable | ⚠️ 部分 | `chunks/*.js` 正确为 `public, max-age=31536000, immutable`；但 manifest 文件无该头（且 404） |
| 不存在资源返回 404 | ✅ | 404（未被 catch-all 兜成 200） |
| JS MIME | ⚠️ 取样命中 404 文件 | 可达的 chunk 为 `application/javascript`（正确） |

**失败的 2 个文件**（切换后 404）：

```
/_next/static/9ScjZUpRU3g5Wt9F1Uedh/_buildManifest.js   -> 404 (text/html)
/_next/static/9ScjZUpRU3g5Wt9F1Uedh/_ssgManifest.js    -> 404 (text/html)
```

**可达的 3 个文件**（切换后 200，cache-control 正确）：

```
/_next/static/chunks/1255-f456c2191ed027a9.js   -> 200 application/javascript immutable
/_next/static/chunks/1646.a93085a0445ba909.js   -> 200 application/javascript immutable
（第三个同理）
```

## 2. 根因

Next.js 静态导出有两类路径：

1. **内容哈希路径**：`/_next/static/chunks/<name>-<contenthash>.js`
   → 不同构建的文件名不同，**天然不冲突**；旧构建的 chunk 在新版本的 `out/` 里不存在，但因为 `REL_ROOT/releases/<OLD_BID>/` 仍保留，
   Nginx 的 `root current`（符号链接）指向新版本，导致旧 chunk 只能通过旧版本目录取到——**而当前 Nginx 配置不暴露旧版本目录**。

2. **构建 ID 前缀路径**：`/_next/static/<BUILD_ID>/_buildManifest.js`、`_ssgManifest.js`
   → 路径含**构建 ID**（本例 `9ScjZUpRU3g5Wt9F1Uedh`）。新版本的 `out/` 里只有新 BUILD_ID 目录，旧 BUILD_ID 目录**随旧版本目录一起被隔离**。

因此：**旧标签页持有的 `_buildManifest.js` URL 在切换后 404**，这会导致旧标签页里的 RSC 预取/客户端导航失败（典型表现是点击链接整页刷新或报 chunk 加载错误）。

任务卡原文已准确预言这一点：「仅保留旧目录尚不能提供旧chunk」。

## 3. 修复方案（三选一，推荐 A+B 组合）

### 方案 A：发布时把旧版本的 `_next/static` 合并/软链进新版本的 static 根（推荐）

在 `release-web-v2.sh` 的切换步骤中，切换前把**上一个版本的 `_next/static/<OLD_BUILD_ID>` 目录**软链到新版本：

```bash
# 切换后，把历史版本的 BUILD_ID 目录补进当前版本的 _next/static/
for old in $(ls "$REL_ROOT" | grep -v '^\.' | grep -v "$BID"); do
  old_static="$REL_ROOT/$old/_next/static"
  [ -d "$old_static" ] || continue
  for d in "$old_static"/*/; do
    name=$(basename "$d")
    [ -e "$CURRENT/_next/static/$name" ] || ln -s "$d" "$CURRENT/_next/static/$name"
  done
done
```

- 优点：旧标签页的 manifest 与 chunk 全部可达；新版本目录仍是自洽的（软链指向 releases 内的保留版本）。
- 注意：软链需在 `ln -s` 后用 `readlink` 校验未逃出 `REL_ROOT`；KEEP_VERSIONS 回收时需清理**指向已删除版本**的软链。

### 方案 B：Nginx `location ^~ /_next/static/` 加 fallback

```nginx
location ^~ /_next/static/ {
    add_header Cache-Control "public, max-age=31536000, immutable";
    try_files $uri @old_static;
}
location @old_static {
    # 按顺序在保留版本里找同一个相对路径
    root /path/to/releases;
    try_files /<v1>/$uri /<v2>/$uri =404;   # 需固定列出版本，或使用 map
}
```

- 缺点：版本列表需静态生成，运维复杂度高；不如方案 A 干净。

### 方案 C：接受 404 并在前端处理（不推荐）

旧标签页捕获 chunk 加载失败后提示"版本已更新，请刷新"。这只能缓解，不能解决 RSC 预取失败。

## 4. 验收标准（修复后需重跑本演练）

| 检查项 | 目标 |
|---|---|
| `old_chunks_reachable_after_switch` | **5/5（含 manifest）** |
| `hashed_asset_immutable` | 所有 `_next/static/**` 均为 immutable |
| `js_mime` / `css_mime` | 正确 |
| `missing_asset_404` | 仍为 404 |
| 旧标签页导航 | 浏览器实测：切换后旧标签页点击站内链接不报 chunk 错（需 Playwright 场景） |

## 5. 实施记录（方案 A，已上线到发布脚本）

`release/release-web-v2.sh` 新增两个函数：

1. `link_old_static()`（插在原子切换之前）：遍历 `REL_ROOT` 内所有保留版本，把每个版本
   `_next/static/<BUILD_ID>/` 目录以软链补进新版本；跳过 `chunks|css|media|webpack` 公共目录，
   且校验软链目标必须在 `REL_ROOT` 内（防逃逸）。
2. `prune_dangling_static_links()`（保留策略之后）：回收旧版本后清理指向已删除目录的断链。

### 修复后实测（真实 Nginx + 两代真实构建）

| 检查项 | 修复前 | 修复后 |
|---|---|---|
| html_served_after_switch | ✅ | ✅ |
| html_not_long_cached | ✅ | ✅ |
| **old_chunks_reachable_after_switch** | ❌ 3/5 | **✅ 5/5** |
| **hashed_asset_immutable** | ❌（manifest 无头） | **✅ 全部 immutable** |
| missing_asset_404 | ✅ | ✅ |
| js_mime | ❌（取样命中 404） | **✅ application/javascript** |

软链实证（`current/_next/static/`）：
```
9ScjZUpRU3g5Wt9F1Uedh -> .../releases/9ScjZUpRU3g5Wt9F1Uedh/_next/static/9ScjZUpRU3g5Wt9F1Uedh/
```

合成发布测试（Windows，`test-release-v2.sh`）：**18 PASS / 0 FAIL / 1 SKIP**（符号链接用例需 Linux）。

## 6. 仍未做

- 旧标签页跨发布的**浏览器级**验收（Playwright：切换前开 tab A，发布后在该 tab 内导航，确认无 chunk 加载错误）。
  静态可达性已证，但 RSC 客户端导航仍需真实浏览器场景；
- `media/`、`css/` 等其他目录的跨版本兼容未单独验证（Next 导出中这些目录通常也用内容哈希，实测 chunks 已覆盖）。
