# 发版与签名

智汇网关的发布包走 **Ed25519 双门禁**：生产启动前会验签名、再逐文件比对哈希，
任一不符直接拒绝启动。这样即使有人改了磁盘上的文件，也起不来。

## 两条独立的门禁

| 门禁 | 覆盖范围 | 清单文件 | 签名文件 |
|---|---|---|---|
| 源码门禁 | 后端与部署脚本 | `deploy/managed-release.json` | `deploy/managed-release.sig` |
| 前端门禁 | 静态导出产物 | `<web-root>/manifest.sha256` | `<web-root>/manifest.sig` |

- 校验器：`deploy/source_preflight.py`、`deploy/web_preflight.py`
- 信任锚：`/opt/workbuddy-manager/release-trust-ed25519.pem`（公钥，644）
- 验签方式：`openssl pkeyutl -verify -pubin -inkey <trust> -rawin -in <manifest> -sigfile <sig>`

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 通过（或「没有清单」—— 旧安装，打印 WARN 放行） |
| 2 | 信任锚缺失或为软链 |
| 3 | 签名校验失败（产物被改过，或签名与清单不配套） |
| 4 | 清单格式错误 / 逐文件哈希不符 |

## 「没有清单就放行」是刻意设计

部署顺序是「先装校验器与 ExecStartPre，再发布带签名的产物」。若「无清单即拒绝」，
中间态就没法引导。所以判据是：

- **没有**清单 → 打印 WARN 并放行；
- **有**清单 → 必须签名有效 + 逐文件哈希一致，否则拒绝启动。

这里的关键是：**清单存在，就等于产物声明「我受门禁保护」**。此后任何改动都必须重新签名。
若有人删掉清单来绕过，nginx 服务的仍是被改过的文件，但「清单缺失」会在启动日志留 WARN，
运维侧可见。

## 改代码后的正确流程

```bash
# 1) 改文件
# 2) 重算清单哈希并重新签名（--rebuild 只更新已有条目的哈希，不新增文件）
REL=/opt/workbuddy-manager/releases/<version>
sudo $REL/deploy/sign-release-ed25519.py \
  --manifest $REL/deploy/managed-release.json \
  --root $REL \
  --key /opt/zhihui-release/keys/release-priv.pem \
  --rebuild

# 3) 先验签，再重启
sudo $REL/deploy/sign-release-ed25519.py \
  --manifest $REL/deploy/managed-release.json --root $REL --verify
sudo systemctl restart workbuddy-web
```

> **新增文件时 `--rebuild` 不会把它加进清单**，需要用 `--add`。新增了 `server/` 下的模块
> 却忘了 `--add`，会出现「文件在磁盘上存在、但不在清单里」—— 门禁不管它，等于多了一个
> 不受保护的入口。

## 私钥保管

私钥在 `/opt/zhihui-release/keys/release-priv.pem`（600，仅 root 可读）。
**能拿到本机 root 的人就能签发布包** —— 这是这条链的边界，不是漏洞。真正的高保障需要
把私钥移到离线机或 HSM，本机只留公钥。

签名时临时拷入、用完立即删除，是目前可接受的折中。

## 已知边界

- 门禁防的是「磁盘文件被改」，**不防**「有 root 的人重新签一份」。
- 前端门禁校验的是 nginx 实际服务的那棵树（软链指向同一目录），所以两者不会漂移；
  但如果直接把产物复制到另一个目录而没走发布流程，那棵树就完全没有保护。
