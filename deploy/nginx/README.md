# 反代配置里两处必须手工同步的地方

站点配置不在仓库里（它在服务器的 `sites-enabled`），但它有两处**会随功能演进**
的约定。这个目录存放其中可版本化的部分，并记录另一处的规则。

---

## 一、余额协议放行片段（`location = /api/usage/token{/}`）

### 它解决的问题

控制台的 `/api/` 段带 `auth_request /__zh_session`（面板会话校验），而余额协议
`GET /api/usage/token` 是**密钥鉴权**、给第三方面板调用的：

```bash
curl -H "Authorization: Bearer sk-xxxx" https://你的域名/api/usage/token
```

若不单独放行，外部面板拿到的是「未登录」（有的配置会 302 到登录页），于是永远
显示「未知余额」。2026-11-23 上线这个端点时正是这么撞上的。

### 怎么用

在站点的 `server {}` 里，**放在 `location /api/ {` 之前**（`=` 精确匹配优先级
最高，位置其实不敏感，但放在前面更好读）：

```nginx
    location = /api/usage/token {
        include /etc/nginx/snippets/zh-usage-token.location.conf;
    }
    location = /api/usage/token/ {
        include /etc/nginx/snippets/zh-usage-token.location.conf;
    }
```

把 `zh-usage-token.location.conf` 放到 `/etc/nginx/snippets/`，然后
`nginx -t && systemctl reload nginx`。

### 两个必须注意的点

1. **`limit_req zone=zh_api_ip_rate`** 是本项目线上用到的限流 zone。你自己的站点
   没有这个 zone 时，删掉那两行，否则 `nginx -t` 会报 `unknown limit_req zone`。
2. **恢复 nginx 备份会让它静默失效。** 这个片段不在 `sites-enabled` 的主文件里，
   而是被 include 进来的；从旧备份恢复主文件后，两段 `location` 就没了，而页面
   一切正常、只有余额接口悄悄回到「未登录」。恢复备份后请重新插入这两段。

`deploy/release.py verify --nginx /etc/nginx/sites-enabled/<站点>` 会检查这两段
是否存在，可在发布前顺手跑一次。

---

## 二、控制台页面路径白名单（**新增页面时必看**）

控制台的每个页面路径都在站点配置里**逐个列名**：

```nginx
location ~ ^/(dashboard|accounts|tasks|keys|models|playground|stats|logs|traffic
             |security|settings|red-packets|profile|claim|login|portal|members
             |agents|alerts)(/.*)?$ {
    root /var/www/zhihui-console-current;
    try_files $uri $uri/index.html $uri.html =404;
    ...
}
```

这不是通配 `location /`，而是白名单：`location /` 指向首页目录
（`/var/www/zhihui-home`），控制台产物在另一处。所以**新增一个控制台页面，
若名字没加进这个正则，线上就是 404** —— 而本地 `npm run build:export` 一切正常。

2026-11-23 新增「调用轨迹」页时正是这么撞上的：产物里明明有
`web/out/traffic/index.html`，线上却 404。

### 新增页面时的步骤

1. 把页面名加进上面那条正则（放在同类页面旁边，便于阅读）：`|logs|traffic|security|`。
2. `nginx -t` 通过后 `systemctl reload nginx`。
3. 同步前端产物到 `/var/www/zhihui-console-releases/<新目录>/`，并把
   `/var/www/zhihui-console-current` 指过去；`_next` 同时同步到
   `/var/www/zhihui-next-pool/`。
4. 直接 `curl -o /dev/null -w '%{http_code}'` 新路径确认 200，**不要只看首页**。

### 为什么不做成通配

控制台与首页共用一个域名，首页是纯静态落地页，控制台是另一个根目录。用通配会让
两者的 `try_files` 互相干扰，且把控制台产物暴露在任意路径下。白名单多一点维护
成本，但边界清楚。

---

## 三、推理路径走滚动中继（**发布不再打断在途调用**）

### 它解决的问题

`systemctl restart` 实测有约 6 秒空窗：停旧进程 → 起新解释器 → 校验整份发布
签名 → 初始化应用。这段时间从 nginx 转发过来的推理请求 connect 直接被拒，
2026-11-24 19:19 那次部署因此丢掉了 4 个真实调用（503 + 三条 502）。

滚动中继（`server/relay_app.py`，listen `127.0.0.1:7867`）按签名路由文件决定把
新请求转到哪个后端实例，已开始的流留原实例不动。中继自己还会在 connect 失败时
等后端回来（4 次尝试、累计 9 秒），正好盖住那 6 秒空窗。

### 怎么用

把 `zh-relay-inference.location.conf` 放到 `/etc/nginx/snippets/`，然后在站点的
`server {}` 里、`location /v1/ {` 之前插入（正则 location 优先级高于前缀，位置
其实不敏感，放前面更好读）：

```nginx
    location ~ ^/(v1/chat/completions|v2/chat/completions|v1/responses|v1/messages|v1/messages/count_tokens|v1/models|responses)$ {
        include /etc/nginx/snippets/zh-relay-inference.location.conf;
    }
```

然后 `nginx -t && systemctl reload nginx`。

### 三个必须注意的点

1. **`$zhihui_relay_upstream` 必须已定义**（`conf.d/zhihui-relay-upstream.conf`
   里 `map $host` 到 `http://127.0.0.1:7867`）。没有它 `nginx -t` 会报
   `unknown "zhihui_relay_upstream" variable`。
2. **中继只认白名单里的路径**，白名单外的 `/v1/*`（如 `embeddings`）会落到
   站点的 `location /v1/` 直连，这是设计如此，不是漏配。
3. **顺序很重要**：先部署新版本、再重启中继、最后接上这两段。若先接上中继而
   中继还跑着旧代码，发布仍会 502 —— 中继没有重试就只是多了一跳。

### 怎么确认真的走了中继

中继与后端在线路上的行为一致（同样的 401/404），看状态码区分不出来。可信的
判据是：中继停掉时推理请求会失败，恢复后立刻正常；或对比 `curl` 直连 `7866`
与走域名两者响应体相同、而把路由文件切到未监听的后端时只有走域名的那一侧失败。


