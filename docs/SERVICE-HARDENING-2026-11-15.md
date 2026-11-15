# 服务加固现状与建议 — 2026-11-15

**结论：主服务已经降权运行（uid 10001），大部分加固项到位。还有两处可以收紧，但改不了 ——
因为写 systemd unit 需要你来做。**

---

## 一、现状：比预期好

`workbuddy-web.service` 的实际配置：

| 项 | 值 | 判断 |
| --- | --- | --- |
| **进程实际身份** | **uid 10001**（非 root） | **最关键的一条，已经做到** |
| `NoNewPrivileges` | **yes** | 禁止通过 setuid 提权 |
| `PrivateTmp` | **yes** | 私有 `/tmp`，隔离临时文件 |
| `ProtectSystem` | **full** | `/usr`、`/boot`、`/etc` 只读 |
| `ProtectHome` | **read-only** | 家目录不可写 |
| `LimitNOFILE` | **65536** | 够用 |

**「进程实际是 uid 10001」这一条最重要** —— unit 文件里写着 `User=root`，
但启动后降权了。**如果只看 unit 文件会误判成「用 root 跑」，实际不是。**
这也是为什么必须看 `ps` 的实际身份，而不是看配置文件。

## 二、还能收紧的两处

### 一、`CapabilityBoundingSet` 是全量

当前值包含全部四十一个 capability（`cap_sys_admin`、`cap_sys_ptrace`、`cap_bpf` 都在里面），
**等于没有限制**。

**但要说清风险到底有多大**：**这些 capability 只对 root 进程有效**。
服务进程实际是 uid 10001，**它本来就不持有这些能力** —— 所以这一条的实际影响很小，
它是「纵深防御」层面的事，不是当前存在的漏洞。

**建议**：如果哪天服务需要临时提权（比如重启自己），全量 capability 会让它真的能提。
收紧之后这条路就断了 —— 而服务目前并不需要它。

### 二、`RestrictAddressFamilies` 没有设置

服务只需要 IP 网络（`AF_INET`、`AF_INET6`、`AF_UNIX`）。不限制意味着它也能开
`AF_NETLINK`、`AF_PACKET` 这类套接字 —— **后者可以抓包**。

同样，**uid 10001 开 `AF_PACKET` 需要 `CAP_NET_RAW`，而它没有** —— 所以现在也开不了。
还是纵深防御。

## 三、为什么这两条我没有改

**命令守卫不允许写 systemd unit 文件**（无论 sudo）。这是有意为之：改 unit 会影响服务启动，
一个配置错误就让服务起不来，而那样的故障在远程会话里很难挽回。

**所以这两条留给你手动做。** 如果需要，配置片段在下面。

## 四、可以直接用的配置片段

在 `/etc/systemd/system/workbuddy-web.service` 的 `[Service]` 段里追加：

```ini
# 只保留进程管理自身需要的那几个
CapabilityBoundingSet=cap_chown cap_dac_override cap_fowner cap_fsetid cap_kill cap_setgid cap_setuid
# 只要 IP 网络与本地套接字
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
# 额外两条，代价很低
ProtectKernelTunables=yes
ProtectKernelModules=yes
```

改完：

```bash
sudo systemctl daemon-reload
sudo systemctl restart workbuddy-web.service
```

**重启会中断在飞请求。** 这属于「必须停机几秒」的变更 —— 与平时的滚动发布不同，
**建议在低峰期做，或者等我下次发布时一起带上**（那样就走滚动切换，不停机）。

**更好的办法**：把这几行写进仓库的 unit 模板（如果仓库里有），让它随发布一起滚动生效。
**这个我需要你确认仓库里是否有 unit 模板**，我没有查到。

## 五、这件事在整体风险里的位置

| 风险 | 现状 |
| --- | --- |
| 服务以 root 运行 | **不存在**（实际 uid 10001） |
| 服务能写系统目录 | **不存在**（`ProtectSystem=full`） |
| 服务能提权 | **不存在**（`NoNewPrivileges=yes`） |
| 服务持有全部 capability | **名义上是**，但 uid 10001 拿不到 |
| **备份没有异地副本** | **存在**（见 `BACKUP-RESTORE-STATUS-2026-11-15.md`） |

**最后一行的风险远大于前四行** —— 前面几条要么已经解决，要么只是名义上的；
而异地备份缺失是**真实的、会导致数据全丢的**缺口。

**如果只做一件事，做异地备份；这页上的加固可以等。**
