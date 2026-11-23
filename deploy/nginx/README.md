# 余额协议的反代放行片段（`location = /api/usage/token{/}` 用）

## 它解决的问题

控制台的 `/api/` 段带 `auth_request /__zh_session`（面板会话校验），而余额协议
`GET /api/usage/token` 是**密钥鉴权**、给第三方面板调用的：

```bash
curl -H "Authorization: Bearer sk-xxxx" https://你的域名/api/usage/token
```

若不单独放行，外部面板拿到的是「未登录」（有的配置会 302 到登录页），于是永远
显示「未知余额」。2026-11-23 上线这个端点时正是这么撞上的。

## 怎么用

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

## 两个必须注意的点

1. **`limit_req zone=zh_api_ip_rate`** 是本项目线上用到的限流 zone。你自己的站点
   没有这个 zone 时，删掉那两行，否则 `nginx -t` 会报
   `unknown limit_req zone`。
2. **恢复 nginx 备份会让它静默失效。** 这个片段不在 `sites-enabled` 的主文件里，
   而是被 include 进来的；从旧备份恢复主文件后，两段 `location` 就没了，而页面
   一切正常、只有余额接口悄悄回到「未登录」。恢复备份后请重新插入这两段。

`deploy/release.py verify --nginx /etc/nginx/sites-enabled/<站点>` 会检查这两段
是否存在，可在发布前顺手跑一次。
