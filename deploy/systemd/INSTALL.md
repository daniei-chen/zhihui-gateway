# 用 socket activation 消掉「重启期间的 502」

## 这是干什么的

现在每次 `systemctl restart workbuddy-web` 都有几秒没有任何进程在监听 7866。
这几秒里的新连接被内核直接拒绝（`ECONNREFUSED`），nginx 记 502 并原样回给客户端。
2026-11-24 两次升级实测影响了 7 条 `/v1/chat/completions`。

装上这套单元之后，**监听 socket 由 systemd 一直持有**，与服务进程生命周期解耦：
重启期间到达的连接留在内核 backlog 里排队，新进程起来后立刻被 accept。客户端只是
慢一点点，不会失败。附带好处：服务进程崩溃时不是"全站 502"，而是"排队等重启"。

## 前置条件

* 服务已经用 `python -m server.serve` 启动（本仓库的发行版即是）。
* 该版本包含 `server/serve.py` 里的 `inherited_listener()`（v1.5.46 起）。
  旧版本没有这段逻辑，装了会因为拿不到 fd 而**仍然自己 bind**，白装。

## 安装（一次性，约 1 分钟）

```bash
# 1) 放单元文件（用发行目录里的副本，保证与代码版本一致）
sudo cp /opt/workbuddy-manager/releases/current/deploy/systemd/workbuddy-web.socket \
        /etc/systemd/system/workbuddy-web.socket
sudo install -d -m 0755 /etc/systemd/system/workbuddy-web.service.d
sudo cp /opt/workbuddy-manager/releases/current/deploy/systemd/workbuddy-web.service.d/10-socket-activation.conf \
        /etc/systemd/system/workbuddy-web.service.d/

# 2) 语法检查（出错会直接打印，不会动运行中的服务）
sudo systemd-analyze verify /etc/systemd/system/workbuddy-web.socket

# 3) 生效：先让 socket 起来，再重启服务（此时服务才第一次拿到 fd）
sudo systemctl daemon-reload
sudo systemctl enable --now workbuddy-web.socket
sudo systemctl restart workbuddy-web

# 4) 验证「真的在用继承来的 fd」
PID=$(systemctl show workbuddy-web -p MainPID --value)

# a) 进程里确实有 LISTEN_FDS=1（这是 systemd 注入到进程环境的，不是 unit 配置项，
#    所以用 systemctl show -p Environment 看不到，必须读 /proc）
sudo tr '\0' '\n' < /proc/$PID/environ | grep -E '^LISTEN_(FDS|PID)='
# 期望：LISTEN_FDS=1  LISTEN_PID=<上面那个 PID>

# b) fd 3 是 socket —— 这是最硬的证据
sudo ls -l /proc/$PID/fd/3
# 期望：... -> socket:[12345678]

# c) 监听 socket 归 systemd 持有（所以服务重启时它还在）
sudo ss -lntp | grep 7866
# 期望：users:(("systemd",pid=1,...))
```

判据：**a 和 b 都有**才算真的生效。只看 `ss` 不够 —— 服务自己也 bind 时 `ss` 一样有 7866。

## 验证真的不再 502（建议真做一次）

```bash
# 另一个终端：每 0.2 秒打一次 /healthz（不需要鉴权），统计状态码
while true; do
  printf '%s %s\n' "$(date +%H:%M:%S)" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://zhihui.kaogong.art/healthz)"
  sleep 0.2
done | tee /tmp/restart-probe.log
```

然后在服务端执行 `sudo systemctl restart workbuddy-web`。

* 装之前：探针里会出现几条 `502`（正是要消掉的东西）。
* 装之后：期望**没有 502**，最多看到一两秒的 `200` 之间出现较长耗时。

## 回滚（10 秒）

```bash
sudo rm -f /etc/systemd/system/workbuddy-web.service.d/10-socket-activation.conf
sudo systemctl disable --now workbuddy-web.socket
sudo systemctl daemon-reload
sudo systemctl restart workbuddy-web
sudo ss -lntp | grep 7866    # 应只剩 python 在监听，行为回到装之前
```

回滚只删 drop-in 与 socket 单元，**不动服务本身**，所以不会让服务起不来。

## 为什么不加 `Requires=workbuddy-web.socket`

加了之后，一旦 socket 单元起不来（端口被占、SELinux、单元文件放错位置），
`Requires=` 会让**服务直接拒绝启动** —— 等于"装个优化补丁把服务装挂"。
当前实现是「拿得到 fd 就用，拿不到就自己 bind」，最坏情况退化成装之前的行为。

## 已知限制

* `.socket` 固定监听 `127.0.0.1:7866`。改端口要同时改这里和
  `WB_MANAGER_PORT`，否则服务会自己 bind 到新端口、socket 还占着旧口。
* 如果将来改成多进程（`--workers N`），这套仍然可用，但 `Backlog` 要一起调大。
  **现在不行**：`python -m server.serve` 直接拒绝 `--workers` 大于 1，因为限额与
  准入计数都在进程内存里，多进程会把额度按 N 放大并让账号读改写重新变成多写者。
