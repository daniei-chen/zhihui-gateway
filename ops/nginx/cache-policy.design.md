# K33 候选：静态资源缓存与旧版本兼容（未部署）

本文是设计说明，不能作为 Nginx include；原 `.conf` 扩展名会误导部署，已更正。

配套 `release/release-web-v2.sh`。本片段目前只设置缓存头，**尚未实现旧资源查找**，不能作为 K33 验收证据。

## 2026-10-25 K37 更新：缓存头已落地为可 include 的片段

上面的「候选片段」已被两个真实文件取代（**不要在 location 里重复写 add_header**，
原因见下一节）：

| 文件 | 上下文 | 作用 |
|---|---|---|
| `static-cache-map.http.conf` | `http { }` | `map $uri $zh_cache_control`：`/_next/static/**` → 一年 immutable；favicon/图片/字体 → 一天；HTML 与 `.txt` → `no-cache` |
| `static-cache.server.conf` | `server { }` | 唯一一条 `add_header Cache-Control $zh_cache_control always` |

### 为什么不是「在 location 里加 add_header」

Nginx 的规则：**location 里只要出现 `add_header`，server 级的 `add_header` 全部
不再继承**（N37 踩过的坑）。旧 fixture 的 `location /` 与 `location ~ \.txt$`
各自写了 `add_header Cache-Control "no-store"`，于是想给 `/_next/` 加强缓存就得
再写一条 location 级 add_header —— 而它会把 server 级的安全头
（`nosniff` / `DENY` / `Referrer-Policy` / CSP）一起挤掉，加缓存反而制造安全回归。

实测证据（VM 内 nginx 1.24，`evidence/r2-k37/nginx-cache.json`）：
- 用 map + server 级 add_header：`/_next/static/*` 得到 immutable，**且** 安全头
  `x-content-type-options: nosniff` / `referrer-policy: no-referrer` 同时存在；
- 反向用例（location 里写 add_header）：`x-content-type-options` **为空** ——
  这就是不用 location 方案的实测理由。

### 部署方式

```nginx
http {
    include /etc/nginx/snippets/zh-static-cache-map.http.conf;   # 复制自 ops/nginx/
    server {
        # …既有安全头 add_header 保持不动…
        include /etc/nginx/snippets/zh-static-cache.server.conf;
        # 若某个 location 里已有 add_header Cache-Control "no-store"，必须删掉它
    }
}
```

改完必须实测（不能只看文本）：

```bash
curl -sI https://<host>/_next/static/chunks/<any>.js | grep -i cache-control   # immutable
curl -sI https://<host>/                              | grep -i cache-control   # no-cache
curl -sI https://<host>/_next/static/chunks/<any>.js | grep -i x-content-type-options  # nosniff
```

### 哪一层负责缓存头（K37 路径归属）

- **生产与本地 fixture**：HTML 与 `/_next/**` 由 **Nginx 直接 `root` 返回**，
  不经过 Manager —— 所以 Manager 的 `cache_headers` 中间件在这条路径上不生效，
  缺口在 Nginx（本节的两个片段就是补这里）。
- **经 Manager 的路径**（本地开发 / 直连 7864）：`manager/server/main.py` 的
  `cache_headers` 中间件本来就正确（实测 6/6 头精确匹配，见
  `evidence/r2-k37/manager-cache.json`），本轮补了回归测试
  `manager/server/tests/test_static_cache_headers.py`。


## 现状风险

- `zhihui-console-current` 只切软链；若旧 release 目录被清理，旧标签页请求旧 hash chunk 会 404；
- RSC payload（`.txt`）是客户端导航产物，不能按"调试残留"删除；
- HTML 必须 no-cache（每次回源校验），带 hash 的资源可以长缓存。

## 候选片段

```nginx
# HTML 文档：每次回源校验，避免旧页面
location ~ ^/(?:.*/)?(?:index\.html)?$ {
    add_header Cache-Control "no-cache, must-revalidate" always;
}

# 带内容哈希的构建产物：可长期强缓存
location ^~ /_next/static/ {
    add_header Cache-Control "public, max-age=31536000, immutable" always;
    # 安全头与主站一致
    include /etc/nginx/snippets/sec-headers.conf;
}

# RSC / 导航 payload：短缓存 + 允许回源（不要长缓存，否则发布后拿到旧 payload）
location ~ \.txt$ {
    add_header Cache-Control "no-cache" always;
    try_files $uri =404;
}
```

## 旧版本兼容策略（与 release v2 配合）

1. `KEEP_VERSIONS` 在脚本里表示保留的非 current 版本数量，默认 2，即总共至少 3 份。
   仅保留目录不会使 current 根目录自动找到旧 chunk。仍须实现经过哈希冲突校验的共享资源归档，
   或版本前缀/明确旧版本回源路径，再以旧标签页和真实 Nginx 验证；
2. 清理只删"非 current 且超过保留期"的版本，且清理前确认该版本不再被访问（访问日志）；
3. 若未来切到"版本前缀 URL"（`/_v/<bid>/...`），需要前端构建同步改 assetPrefix —— 本阶段不做。

## 验收

- 发布后旧标签页继续导航、复制、打开历史对话（需浏览器）；
- 新标签页拿到新版本；
- `curl -I` 检查 `/_next/static/*` 返回 immutable、HTML 返回 no-cache。

## 未验证

- 需要在真实 Nginx 上验证 location 优先级（`^~` 与正则的匹配顺序）；
- 未做浏览器级旧标签页测试。
