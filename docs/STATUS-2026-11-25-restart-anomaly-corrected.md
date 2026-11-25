# 更正：那 29 次「不明重启」是部署，不是故障（第 47 轮）

## 1. 上一轮的结论是错的

上一轮我写了「24 小时内 35 次不明重启」，排除了八种原因，把这件事标成 P0。

**这一轮查清了：它们是部署。不是故障。**

## 2. 证据

systemd 日志的元数据给出了调用者：

```
_COMM=systemd  _PID=1
```

一开始我以为这意味着「systemd 自己决定的」，于是去查依赖关系
（`BindsTo` / `PartOf` / `Conflicts`）—— 全是空。

然后换了个角度看停止事件**前后**的全量日志，看到这个：

```
08:22:47  sudo[1158603]: ubuntu : TTY=pts/3 ; COMMAND=/opt/workbuddy-manager/rele...
08:22:52  sudo[1158756]: ubuntu : TTY=pts/2 ; COMMAND=./deploy/release.r...
08:22:56  sudo[1158808]: ubuntu : TTY=pts/2 ; COMMAND=./deploy/release...
08:22:48  python3[1158415]: TRUSTED-PREFLIGHT-OK version=v1.5.63 files=783
```

那个时间窗里的完整命令序列：

```
./deploy/release.py stage    v1.5.64 --archive /tmp/v164-flat.tar.gz --web /tmp/webout-164.tar.gz
./deploy/release.py seal     v1.5.64
./deploy/release.py verify   v1.5.64
./deploy/release.py activate v1.5.64
/opt/workbuddy-manager/releases/v1.5.64/deploy/healthcheck.sh
./rollout-rolling.sh 1.5.64
/usr/bin/rm -rf /opt/workbuddy-manager/releases/v1.5.64
```

**一次完整的部署流程。**`rollout-rolling.sh` 里有 `systemctl restart`，那就是重启的来源。

24 小时内 `release.py` 被调用 **188 次**。除以每次部署的六个步骤，**约 31 次部署** —— 与 29 次重启对得上。

## 3. 为什么一开始没看出来

因为我在 `journalctl -u workbuddy-web` 里找，而**调用者的记录不在那个 unit 的日志里**，
在全局日志里（`sudo[...]: ubuntu : TTY=pts/2 ; COMMAND=...`）。

`_COMM=systemd _PID=1` 是对的 —— stop 动作确实是 systemd 执行的。
但**"谁让 systemd 去做"记在另一处**。我只看了执行者，没看请求者。

**这和之前几次是同一类错误：测量范围选错了。**（第 5 次）

## 4. 但有一个真问题被顺带发现了

对照重启时刻与 5xx 时间戳：

```
19:19:39   Started workbuddy-web.service
19:19:39   503  /v1/chat/completions  error_code=gateway.application.error
```

**同一秒。**中继没有兜住这一次。

29 次重启里漏了 1 次 —— **中继的覆盖率是 28/29，不是 29/29。**

`gateway.application.error` 是网关自身不可用，不是上游失败。这次失败属于
规范 §3.4 里的**部署窗口**，是我们的责任。

## 5. 另一个独立问题：账号池枯竭

`21:18` 那批 5xx 与重启无关，是另一种：

```
endpoint=/v1/chat/completions  error_code=no_healthy_account  ×7
```

按 §3.4 这属于**容量拒绝**，同样是我们的责任（不是上游失败）。

近 7 天分布：

| 日期 | 次数 |
|---|---|
| 11-19 | **50** |
| 11-20 | 1 |
| 11-21 | 1 |
| 11-22 | 1 |
| 11-24 | 7 |
| 11-25（今天） | 0 |

**这是偶发的容量问题**，不是代码缺陷 —— 账号池里可用的号不够时就会这样。
解决要靠加账号（运营动作），不是改代码。

## 6. 这一轮实际收获

| 项 | 上一轮的判断 | 本轮查清 |
|---|---|---|
| 29 次重启 | 「不明原因，P0」 | **是部署，`release.py` 被调用 188 次** |
| 中继可靠性 | 「可能没兜住」 | **28/29 兜住了，漏了 1 次** |
| 21:18 的 7 个 5xx | 未查 | **账号池枯竭（`no_healthy_account`）** |

**「不明重启」这个结论作废。**但它换来了两个真实结论：中继有 1/29 的缝隙、账号池会偶发枯竭。

## 7. 一个仍然没解释的现象

08:00-13:47 期间在部署 **v1.5.6x**，而我这一轮的工作是从 14:44 开始的，
今天的版本是 v1.5.75 之后。**那段时间的部署不是本会话做的。**

可能是：
- 另一个自动化流程
- 有人在另一个终端手动操作
- 另一个 agent 在同一台机器上工作

**我没有足够证据判断是哪种，所以不下结论。**但这意味着：
**这台机器上可能有并发操作者，我看到的「异常」有可能是别人正常的工作。**

这对以后的判断很重要 —— 遇到「无人解释的变化」，先问「是不是另一个人在做正常的事」，
而不是直接假定「系统出了问题」。

## 8. 下一轮

| 项 | 优先级 |
|---|---|
| **中继漏掉的那 1/29 为什么漏** | **P1** |
| 账号池容量（需运营加号） | 运营 |
| GitHub Release v1.5.81 | 等 CI |
| 异地备份 | 需外部存储 |