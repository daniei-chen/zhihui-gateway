# 发版检查清单

这份清单是给「下一次发版」用的，不是事后总结。
每一条都来自实际踩过的坑，写在前面是为了不再踩第二次。

## 零、先问三个问题

发版前先停下来问：

1. **这次改动，用户会看见什么变化？** 说不出来就先别发 —— 见 §2.5。
2. **如果没有变化，为什么需要一个新版本号？** 文档、测试、CI 配置的改动不需要发版。
3. **重启窗口里会有真实请求吗？** 如果预计是空窗，那「零中断」这句话不成立 —— 见 §1.4。

## 一、一次发版只做一件事

**2026-11-25 一天发了 30 个版本**（v1.5.62 → v1.5.93），原因是每轮迭代都单独发版。
结果是：版本号跳跃、列表里混着草稿、有的带描述有的只有一个号。

代价不是磁盘空间，是**没人能说清哪个版本有什么**。

规矩：
- 一个版本号对应一件用户可感知的事
- 纯文档、纯测试、纯 CI 的改动**不发版**
- 同一天多轮迭代时，合并到一个版本号里发

## 二、发布前的四道门禁

```bash
python deploy/protected-customizations.py --update   # 仅在改了受保护文件后
python tools/bump_version.py <版本号>
python tools/ci_regression.py                        # 必须 exit 0
```

**顺序有讲究，写错会白跑：**

| 步骤 | 必须在什么之后 | 为什么 |
| --- | --- | --- |
| 提交代码改动 | — | 这是 `source_commit` 要指向的提交 |
| 写 CHANGELOG | 提交代码改动之后 | 写早了会记录旧哈希 |
| `bump_version.py` | **CHANGELOG 写完之后** | 它重算已审阅文件的哈希 |
| `protected-customizations.py --update` | **提交之后** | 它读的是 `HEAD:<file>`，不是工作区 |
| 设 `source_commit` | 全部提交之后 | 指向真正的代码提交 |

**最容易漏的两处：**

1. **`protected-customizations.py --update` 必须在 `git commit` 之后跑。**
   它的 `digest()` 用 `git cat-file blob HEAD:<path>` 取已提交字节 —— 这是为了绕开
   Windows 的 CRLF 转换。所以工作区改了但没提交时，它算出的还是旧哈希，
   会打印「已更新 7 项」但文件一个字节都没变。

2. **CHANGELOG 必须在 `bump_version.py` 之前写。**
   否则 `reviewed_patch_files` 里记的是旧 CHANGELOG 的哈希，门禁直接红。

## 三、发版：只用脚本，不手工敲

```bash
sudo ./deploy/release.py stage <版本> --archive <tar> --web <web tar>
sudo ./deploy/release.py seal <版本>
sudo ./deploy/release.py verify <版本> --nginx <站点配置>
sudo ./deploy/release.py activate <版本> --nginx <站点配置>
sudo ./deploy/rollout-rolling.sh <版本>
```

**注意 `stage` 会拒绝已存在的目录。** 所以增量部署的正确顺序是：

```
复制旧版本目录 → 放新文件 → 打包 → 删掉目录 → stage（从 tar 重建）
```

`activate` 会切换 `<root>/deploy` 这个符号链接，而它指向 `releases/current/deploy` ——
所以切换指针的瞬间，**正在用的脚本也换了**。这就是为什么 `rollout-rolling.sh`
必须用新版跑，而不是旧版。

### 蓝绿：候选实例怎么退场

`route_switch old` 之后，候选实例（`workbuddy-web-helper`，7865）上还留着切流前
接下的请求。脚本不再「等一秒就停」，而是**先等它报在途归零，再停**：

- 主信号是候选实例自己的只读探针 `GET /api/health/inflight`（只在回环地址上应答，
  不带凭据）。计数来自 `DrainMiddleware` 的请求级跟踪，流式响应的整个生命周期都在
  里面 —— `/metrics` 要采集器令牌、`/api/readyz/detail` 要管理员身份，
  而发布脚本是 root 的 shell，两者都拿不到，所以单独开了这个端点。
- 墙钟只是安全上限：`WB_ROLLOUT_DRAIN_TIMEOUT`，默认 300 秒。它必须小于 unit 的
  `TimeoutStopSec=330`，否则等于在等 systemd 的 SIGKILL。
- 计数读不到、上限到了、或者停之后的优雅关闭日志里有
  `timeout graceful shutdown exceeded` / `requests outstanding`，**发布按失败记
  （退出码 2）**，`rollout-log.jsonl` 里写 `候选实例在途未排空`。
  截断的流不会落 5xx，所以不能等那个统计来说话。
- 配套上限：helper unit 的 `WB_GRACEFUL_SHUTDOWN_S=320`（launch_guard 从环境读，
  没设时仍是 75）、`WB_DRAIN_TIMEOUT_SEC=320`、`TimeoutStopSec=330`。
  三个数的关系是 300 < 320 <= 330，`test_rollout_candidate_drain.py` 会校验它。

局限：这套判据依赖候选实例跑的是带 `/api/health/inflight` 的版本。探针读不到时脚本
直接失败（不会把「读不到」当成「排空了」），所以从更老的版本第一次蓝绿发布前，
要先确认候选实例是新版。

## 四、部署后必须验证的五件事

| 项 | 查法 | 通过标准 |
| --- | --- | --- |
| 版本与提交号 | `curl /api/release` | `release_id` 与 `source_commit` 都对 |
| 五个页面 | 逐个 curl | 全部 200 |
| **重启窗口请求数** | 查 `request_outcomes` | **必须 > 0**，否则零中断不成立 |
| **重启窗口 5xx** | 同上，且用 nginx 日志交叉核对 | 0 |
| 健康巡检 | `./deploy/healthcheck.sh` | 问题 0 项 |

**「零中断」这句话只在同时给出请求数时才成立。**
窗口里 0 条请求、0 条 5xx —— 那不是零中断，那是没人在用。

## 五、Release 说明的规范写法

必须有四段：

```markdown
### 修复（或 新增 / 改进）
- **一句话说清用户感知到的变化。**
  原来会怎样：……
  现在会怎样：……

---

### 部署验证
| 项 | 结果 |
|---|---|
| 版本 | vX.Y.Z |
| 构建提交 | `<sha>` |
| 五个页面 | 全部 200 |
| 重启窗口 | 时间（CST） |
| **窗口内请求数** | **N 条** |
| **窗口内 5xx** | **0 条** |
| 健康巡检 | 问题 0 项 |
| 质量门禁 | 后端 N 例 + 前端 N 例，exit 0 |

---

### 这一版没做到的事
（如实写。没有就写「没有」。不要空着。）

---

### 产物校验（sha256）
（从 `gh release view --json assets` 取真实摘要，不要编。）
```

**标题统一为：`vX.Y.Z — <一句话描述>`。** 只有版本号没有描述的 Release 是不合格的。

## 六、资产必须四个

```
workbuddy-manager-vX.Y.Z.tar.gz
workbuddy-manager-vX.Y.Z.zip
WorkBuddyManagerSetup.exe
WorkBuddyManagerSetup.exe.sha256
```

**四个齐了才能 `gh release edit --draft=false`。** 缺一个就留在草稿里等，
但**不要留超过一次迭代**——补不齐就取消构建并删掉草稿，别让它一直挂着。

`v1.5.92` 和 `v1.5.65` 就是因为缺安装包挂了几个小时，最后只能删掉。

## 七、已知的坑

| 坑 | 表现 | 处理 |
| --- | --- | --- |
| `continue-on-error` 兜不住卡住 | 镜像构建步骤挂 5 小时，`installer` 排队永远不跑 | 需要 `timeout-minutes`；当前令牌缺 `workflow` 权限改不了 |
| `curl -sfL` 不报截断 | 496 KB 的文件下成 213 KB，大小对不上才被发现 | 加 `--compressed --max-time 120`，下完比对字节数并验 UTF-8 |
| 旧版本 tag 触发构建会失败 | `workflow_dispatch` 指定较早的 tag 时 checkout 失败 | 只对当前分支上的 tag 触发 |
| `vv1.5.94` 双前缀 | 完成消息里版本号未归一化 | 已在 v1.5.94 修正 |
| 时区 | `strftime('%s','<CST时间>')` 按 UTC 解释，差 8 小时 | 写成 `strftime('%s','<CST时间>','-8 hours')` |

## 八、不要再做的事

- 同一天连发十几个版本
- 留着一半资产的草稿不管
- Release 标题只有版本号
- 在窗口里 0 条请求时声称「零中断」
- 编造 sha256 摘要
- 把测试用例写进 CHANGELOG
- 用「先发后修」绕过门禁
