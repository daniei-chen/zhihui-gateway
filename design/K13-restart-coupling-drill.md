# K13 · 重启耦合实测（lab VM，systemd 255）

> 环境：`zhihui-local-lab`，systemd 255 (255.4-1ubuntu8.18)。
> 用两个合成 unit（`qtg-lab.service` / `engine-lab.service`）复刻生产拓扑：
> engine 带 `PartOf=qtg-lab.service`。
> **生产零变更**：只操作 `/run/systemd/system/` 下的合成 unit。

## 一、结论（先看这个）

| 项 | 结论 |
|---|---|
| `PartOf=` 是否真的传播重启 | **是**，实测 PID 变化 |
| **候选 drop-in（`PartOf=` 置空）是否有效** | **无效 —— 这是本轮最重要的发现** |
| 怎样才能解耦 | **重写主 unit 文件**，去掉 `PartOf=` 行 |
| 是否有别的办法 | `PartOf=` 是列表属性，drop-in 只能**追加**不能**删除** |

## 二、实测数据

### 场景 A：原始状态（engine 带 `PartOf=qtg-lab`）

```
BEFORE  engine MainPID=130243   qtg MainPID=130242
$ systemctl restart qtg-lab
AFTER   engine MainPID=130310   qtg MainPID=130313
```

**引擎被连带重启**（PID 130243 → 130310）。确认 K13 描述的问题成立。

### 场景 B：drop-in 写 `PartOf=`（空值）—— 候选配置的做法

```ini
# /run/systemd/system/engine-lab.service.d/10-decouple.conf
[Unit]
PartOf=
```

```
$ systemctl show -p PartOf engine-lab.service
PartOf=qtg-lab.service          ← 仍然是 qtg！空赋值没有清空
BEFORE  engine MainPID=130310
$ systemctl restart qtg-lab
AFTER   engine MainPID=130425   ← 仍然被连带重启
```

**候选配置完全无效。** 原因：`PartOf` 是**列表（list）属性**，systemd 对列表
属性在 drop-in 里的处理是**追加/覆盖整个列表**，而 `PartOf=` 空值在列表语义下
不表示"清空"，主 unit 的赋值仍然生效。

### 场景 C：验证"追加"语义

```ini
[Unit]
PartOf=engine-lab.service-dummy-nonexistent
```

```
$ systemctl show -p PartOf engine-lab.service
PartOf=qtg-lab.service           ← 追加也没生效（覆盖了整表但主 unit 仍生效）
AFTER restart: engine MainPID=130546  ← 仍被连带
```

### 场景 D：重写**主 unit 文件**（去掉 `PartOf=`）

```
$ systemctl show -p PartOf engine-lab.service
PartOf=                          ← 空，生效
BEFORE  engine MainPID=130655
$ systemctl restart qtg-lab
AFTER   engine MainPID=130655    ← **保持不变**
        qtg MainPID=130662       ← qtg 正常重启
        engine ActiveState=active
```

**这是唯一有效的方式。**

## 三、对候选配置的修正

`ops/systemd/qoder2-engine.service.d/10-decouple-engine.conf` 当前内容是：

```ini
[Unit]
PartOf=
```

**这份候选按原样安装不会有任何效果**（场景 B 已证）。必须改成两条路之一：

### 方案 1（推荐）：直接改主 unit 文件

```bash
# 编辑 /lib/systemd/system/qoder2-engine.service（或 /etc/systemd/system/ 副本）
# 删除或注释掉 `PartOf=qtg.service` 这一行
sudo systemctl daemon-reload
```

代价：主 unit 文件属于**包管理器**，将来包升级会覆盖。所以更稳妥的是
**把主 unit 复制到 `/etc/systemd/system/`**（该路径优先级高于 `/lib`），
在副本里去掉 `PartOf=`，然后 `daemon-reload`。

```bash
sudo cp /lib/systemd/system/qoder2-engine.service /etc/systemd/system/qoder2-engine.service
sudo sed -i '/^PartOf=/d' /etc/systemd/system/qoder2-engine.service
sudo systemctl daemon-reload
systemctl show -p PartOf qoder2-engine.service    # 应为空
```

### 方案 2：改用 `BindsTo=` → 反向

如果实际上希望"引擎停 → QTG 也停"（而不是相反），那是另一套语义，
需要产品确认方向。**不要在没确认前动**。

### 另外：`ConsistsOf` 不用管

`qtg.service` 上的 `ConsistsOf=qoder2-engine.service` 是 systemd 根据
`qoder2-engine` 的 `PartOf=` **自动生成的反向属性**，不能在 qtg 侧写
`ConsistsOf=` 来消除它。去掉引擎的 `PartOf=` 之后，`ConsistsOf` 会自动消失。

## 四、验证脚本（生产执行前跑这段）

```bash
# 1) 记录基线
systemctl show -p MainPID qoder2-engine.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8799/

# 2) 安装候选（方案 1）
sudo cp /lib/systemd/system/qoder2-engine.service /etc/systemd/system/qoder2-engine.service
sudo sed -i '/^PartOf=/d' /etc/systemd/system/qoder2-engine.service
sudo systemctl daemon-reload

# 3) 确认属性真的变了（这一步不能省 —— 本轮的教训）
systemctl show -p PartOf qoder2-engine.service     # 必须是空

# 4) 重启 QTG 并确认引擎 PID 不变
systemctl restart qtg
sleep 2
systemctl show -p MainPID qoder2-engine.service    # 应与步骤 1 相同
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8799/   # 200

# 回退：删掉 /etc/systemd/system/qoder2-engine.service → daemon-reload
```

## 五、未覆盖

1. **生产真实 unit 内容**：本轮用合成 unit 复刻 `PartOf=` 语义，但生产的
   `qoder2-engine.service` 可能还有 `Requires`/`After`/`BindsTo` 等其他耦合，
   必须在目标机 `systemctl cat qoder2-engine.service` 上核对。
2. **冷启动顺序**：本轮只测了 `restart qtg`，没测整机重启后的启动顺序
   （需要重启 VM，且在 VM 里没有真实服务）。
3. **在途流是否真的保持**：合成 unit 用 `sleep` 复刻，没有真实推理流量。
   "重启 QTG 时在途 SSE 不断"需要真实引擎 + 真实推理才可证。
