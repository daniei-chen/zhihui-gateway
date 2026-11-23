# 运维手册

照着做就能完成的事，不写原理。原理在各自的代码注释与 `docs/` 里，这里只保留
**步骤、判据、以及做错时的表现**。

适用部署：单机，nginx + Python 网关（`127.0.0.1:7866`）+ 上游 daemon
（容器 `zhihui-workbuddy`，`127.0.0.1:7863`）。

---

## 1. 发布一个版本

### 前置

本机（打包机）与部署机都需要，缺一个都会卡在半路：

- 本地：仓库干净、门禁全绿（`python tools/ci_regression.py`，UTC 时区）。
- 部署机：`/opt/workbuddy-manager/release-trust-ed25519.pem`（公钥）、
  `/opt/zhihui-release/keys/release-priv.pem`（私钥）、发布镜像
  `trae2api-hub:final4-20261122`（内含 `cryptography`）。

### 步骤

```bash
# 1) 打包（本地）
git tag -a vX.Y.Z <commit> -m "vX.Y.Z"
git push origin vX.Y.Z
git archive --format=tar.gz -o /tmp/vXYZ.tar.gz vX.Y.Z
tar -czf /tmp/webout-XYZ.tar.gz -C web out        # 前端产物（不入库，现场构建）

# 2) 上传
scp /tmp/vXYZ.tar.gz /tmp/webout-XYZ.tar.gz ubuntu@<host>:/tmp/

# 3) 四步流水线（部署机，容器内执行）
RUN='sudo docker run --rm --network none \
  -v /opt/workbuddy-manager/releases:/rel:rw \
  -v /opt/workbuddy-manager/release-trust-ed25519.pem:/trust.pem:ro \
  -v /opt/zhihui-release/keys:/keys:ro -v /etc/nginx:/etc/nginx:ro \
  -v /tmp:/tmp:ro -v <工具目录>:/tools:ro \
  --entrypoint python3 trae2api-hub:final4-20261122 /tools/release.py \
  --root /rel --key /keys/release-priv.pem --trust /trust.pem'

$RUN stage  vX.Y.Z --archive /tmp/vXYZ.tar.gz --web /tmp/webout-XYZ.tar.gz
$RUN seal   vX.Y.Z
$RUN verify vX.Y.Z --nginx /etc/nginx/sites-enabled/<站点>
$RUN activate vX.Y.Z

# 4) 重启并确认（宿主机）
sudo systemctl restart workbuddy-web
sleep 12 && curl -s http://127.0.0.1:7866/api/release
```

### 判据

`/api/release` 必须返回**目标版本**且 `source_commit` 是你打的 tag 的完整哈希。
不一致就是没切换成功，别往下走。

### 做错时的表现

| 现象 | 原因 |
|---|---|
| `WRITER-LAUNCH-BLOCKED: host release/state/runtime contract rejected` | 启动门禁拒绝。**先跑 `preflight-gate.py`**，它会逐项报哪一条不过 |
| 服务起不来但门禁全绿 | 检查 `releases/current` 是不是写成了绝对路径（应为相对名 `vX.Y.Z`） |
| `verify` 报 `release_id != 目录名` | 归档里的元数据版本号没跟着改 |

### 已知的先后顺序陷阱

`docs/protected-customizations.json` **自身也在** `server/release-provenance.json`
的受审清单里。所以同步哈希要跑两遍：先写受保护清单并 `git add`，再算溯源清单。
一遍过会留下「清单哈希是旧值」的错。

---

## 2. 回滚

```bash
$RUN rollback                       # 指针切回上一版（.previous 记录）
sudo systemctl restart workbuddy-web
curl -s http://127.0.0.1:7866/api/release
```

`rollback` 只切指针，不重启 —— 容器里没有 systemd。重启失败时再跑一次
`rollback`，它会把 `.previous` 与当前互换成原来的方向。

**不要把旧版本目录删掉**：回滚的目标就是它。

---

## 3. 抓包（调用轨迹）

### 开启

```bash
sudo systemctl edit workbuddy-web      # 加一行：
#   [Service]
#   Environment=WB_TRAFFIC_TAP=1
sudo systemctl daemon-reload && sudo systemctl restart workbuddy-web
```

环境变量是**总开关**：它为 0 时，控制台里的开关写了什么都不生效（接口会返回
`blocked_by: WB_TRAFFIC_TAP=0`，不会静默成功）。

### 关闭

改回 `0` 并重启，或在控制台关掉面板开关（面板开关只能在总开关打开的前提下收紧）。

### 数据位置与保留

- 目录：`/opt/workbuddy-manager/data/traffic/<日期>/`，单条 `*.json.gz`
- 默认保留 24 小时、最多 5000 条、单条 256 KB 截断
- 清理每小时一次；关闭抓包后目录**不会再增长**，已有记录按保留期删除

### 判据

开启后发一次真实调用，`/traffic` 页面应出现一条记录，点开能看到「客户端请求 /
网关发往上游 / 差异 / 上游响应」四段。若目录一直不出现，先看
`/api/traffic/status` 的 `enabled` 与 `counter`。

---

## 4. 反代配置（两处会随功能演进）

详见 `deploy/nginx/README.md`。摘要：

1. **新增控制台页面**必须把路径加进站点配置的白名单正则，否则线上 404 而本地
   构建一切正常。
2. **余额协议放行片段**是被 include 的；从旧备份恢复站点主文件后它会静默消失
   （页面正常、只有余额接口回到「未登录」）。`release.py verify --nginx` 会检查。

改完一律 `nginx -t` 通过再 `systemctl reload nginx`。

---

## 5. 定期做但不要自动化的事

| 项 | 频率 | 为什么手动 |
|---|---|---|
| 回滚演练 | 每季度 | 会短暂中断服务，需要挑时段；做法见第 2 节，来回各一次重启 |
| 恢复演练（`deploy/restore_drill.py`） | 每月 | 要确认备份文件真实可用；脚本只读演练，不碰生产数据 |
| releases 目录清理 | 磁盘 > 80% 时 | 删除不可逆。保留最近 5 个版本 + 回滚目标 + 当前 |

### releases 清理的安全做法

```bash
sudo du -sh /opt/workbuddy-manager/releases/* | sort -rh | head -10
# 确认要删的版本既不是 current，也不是 .previous 里的那一版
readlink -f /opt/workbuddy-manager/releases/current
cat /opt/workbuddy-manager/releases/.previous
sudo rm -rf /opt/workbuddy-manager/releases/vX.Y.Z
```

控制台产物（`/var/www/zhihui-console-releases/`）同理，但要注意
`/var/www/zhihui-console-current` 指向的那一份不能删。
