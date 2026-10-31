# WorkBuddy / 有界请求体候选

状态：**2026-10-31 已独立切换生产引擎，并完成运行 ELF 与实际限制核验**。
Manager 交互补丁 `.2` 的源码/签名清单不变；此补丁单独发布，不是网页升级器。
`manifest.json` 与 `candidate-verification.json` 保留候选时点，不能改写为历史上已经部署。
最新现场回执见 `production-verification.json`。

## 修复范围

- 保留现有鉴权顺序：无效密钥在读取 body 之前返回401。
- 在任何选号、上游调用和账号状态修改前使用 `http.MaxBytesReader`。
- 声明超限直接413；分块、缺少长度或伪造较小长度仍按实际字节计数。
- 默认单请求64MiB、在途原始 body 总预算128MiB、最多32个持有 body 的请求。
- 容量不足返回503与 `Retry-After: 1`，不排队、不中断其他请求、不惩罚账号。
- 租约持有至整个转发完成，包括长SSE；读完 body 不能提前释放仍在使用的容量。
- 错误、取消和退出路径释放租约；阻塞读取尚未结束时不提前腾出名额。
- 鉴权后的 `/status` 追加实际限制、占用和累计容量拒绝数；不新增公开管理入口。
- 移除 `WB2A_DUMP_REQ` 原始请求落盘开关；读取错误不再把任意 reader 错误文本回传。

预算是原始 payload 长度，不是完整 Go heap 上限。slice余量、JSON、协议变换、压缩及
重试副本仍需要进程/容器内存余量；保留原60秒读时限及系统内存保护，不声称绝对不会OOM。
这也不把32个 body 名额等同于32个付费推理并发或多进程共享限额。

## 来源与可重建

本补丁适用于留存的 `20261028.u2114.1` 已部署引擎构建源码快照，不适用于名字相同的
当前可变 checkout。两棵树在29个Go文件和部分新增测试上有差异；盲目复制会退回已经修复的
渠道拒绝、粘性选号、流式收尾等行为。这里只改变三个已有文件、增加三个文件。

`manifest.json` 固定263个源码/测试/嵌入资源/脚本的哈希、266个候选文件的哈希和patch哈希。
它不包含账号、数据、真实配置、Cookie或口令。MIT与原作者版权见 `LICENSE`。
哈希和历史构建回执能证明输入身份；尚未用历史工具链逐字节复现原ELF，不冒称完整可复现供应链。

准备必须使用只含manifest列出文件的独立导出目录；不允许把生产目录直接当输入。

```sh
python tools/prepare_workbuddy_ingress.py \
  --source /isolated/workbuddy-pinned-source \
  --destination /isolated/workbuddy-guarded-source

cd /isolated/workbuddy-guarded-source
GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local go test -count=1 ./...
GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local go test -race -count=1 \
  -run 'Test(Ingress|BodyGuard|BodyBudget|ChannelRejectionIsRequestScoped)' \
  ./internal/server ./cmd/server
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -buildvcs=false -o wb2api ./cmd/server
```

依赖须预先按 `go.sum` 校验。以上命令不安装依赖、不启用真实供应商E2E标签，也不是生产部署授权替代。
候选目录不覆盖已有目录，失败目录与输入均保留；source发生漂移、链接、额外文件、patch漂移均失败关闭。

## 三层大小合同

| 层 | 本轮现场/候选 | 作用 |
| --- | --- | --- |
| 公网 Nginx | 已上线64MiB | 网络入口保护，继续保留 |
| Manager | 已上线前置鉴权；在途body预算64MiB | 实际大于总预算可能503；上游旧 `server.max_body_mb=1024` 会覆盖单请求环境值256 |
| Go引擎 | 本候选64MiB / 总预算128MiB / 32个租约 | 未上线；旧 `server.max_body_mb` 仍按原契约忽略，新限制来自 `ingress` |

Manager不是所有请求都逐字节透传，协议转换或JSON序列化可能扩大body。因此边缘64MiB不保证
经转换后仍能通过引擎64MiB；若需要更大上下文，必须连同真实转换大小、峰值内存和并发预算一起验收，
不能简单把所有限制改成无限。最终统一配置、面板显示与生产验收仍属于开放项。

配置示例仅为新候选引擎的JSON片段，不覆盖现有配置：

```json
{"ingress":{"max_body_mb":64,"max_buffered_body_mb":128,"max_concurrent_bodies":32}}
```

单请求允许1–512MiB，总预算至少单请求且不超过1024MiB，租约数1–256。
零表示使用有限默认，负值、类型错误及不合法组合拒绝启动。不增加隐藏的环境变量覆盖。

## 已有证据

- 补丁准备器包含基线漂移、链接、越界、并发修改、失败目录保留和Git环境隔离测试；
  真实263→266文件准备、输入未改变和候选哈希匹配。
- Go1.27.0本地交叉构建18个Linux/amd64包，CGO关闭。
- 精确当前引擎基础镜像、network-none、UID10001、cap-drop-all、只读rootfs：
  **1368个顶层测试通过，含子测试1722条PASS，0失败、0跳过**。
- Windows竞态检测20个顶层用例通过（含子测试22条PASS），不是Linux竞态运行证明。
- 原生Linux候选ELF七项HTTP场景通过，覆盖headers-only401/413、chunked413、普通与SSE、
  转发中租约占用/503、完成释放且账号无惩罚。三个合成provider调用，零真实provider调用。
- 候选ELF SHA256：`3360de3f288c454ee8b1d2c80858cfbb3f84f8d13ca4b8988b78566ad8182f87`。
- 独立候选镜像 `sha256:3e6bdac8d0bee609b025045901c466f67557c245ff2d1510318d2e46496de9f7`
  已构建，继承运行配置完全一致，19个`/app`成员仅ELF变化；该镜像本体再次通过七项HTTP验收。
- 测试前后线上Manager PID/签名清单、引擎容器/PID与Nginx哈希保持一致。

失败记录保留：首次缺少两个go:embed资源；首次本地host-checkout试验不作为最终输入；
Windows目录fsync两项失败未豁免，实际Linux全部通过。HTTP验收夹具前两轮分别有generator引用错误、
只查首条消息而漏掉注入system的错误，修复夹具后同一ELF通过，未修改业务实现来迎合测试。

## 实际切换与回退边界

- 独立 Ed25519 审批绑定镜像、ELF、源码和受测控制器；宿主发布/备份锁及角色/日额度底线保持。
- Nginx自然排空后停Manager全部写者；独立看门狗也需暂时停触发并等待结束，旧告警链保留。
- 引擎入站、出站和子任务均核验；避开定时任务窗口，连续65秒无活动连接后才优雅停旧进程。
- 新旧容器共用原bind数据目录，但绝不同时启动；旧容器停机保留且自动重启策略设为no。
- 线上验证401与413在发送body前返回；未为验收调用真实provider、模型或领取任务。
- 首页、原登录、控制台和Manager签名内容不变；预警/对账定时器恢复并通过实际状态核验。

切换过程发生保护拒绝和启动竞态，失败回执没有删除：独立看门狗被遗漏在初始写者清单外，
配置原0660触发权限保护，same-PID提权引导尚未降权时过早检查，以及一次WAL元数据瞬时不一致。
没有豁免writer/权限/SQLite检查；配置仅同inode收紧为0640，新控制器在有界时间内重新完整验证
最终身份，永久不匹配仍失败。该过程包含维护不可用窗口，不宣称零停机。

**仍开放**：Manager历史单请求限制覆盖语义、协议转换膨胀、未来重建来源的声明化、
实际生产镜像回退演练和长期SLO。当前容器无Compose标签，不能用可变host-checkout重建；
保留原镜像并不等同于回退已经演练。只回退代码/镜像，不恢复账号、令牌、账本或历史状态，
也不能跳过现有角色/日额度保护。原始body预算不是全进程内存上限。

这只推进附件P2-1，不能据此关闭未知账务、公开领取、离机灾备、Docker权限或其他引擎供应链问题。
