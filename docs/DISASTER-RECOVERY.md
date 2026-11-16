# 灾难恢复手册

> **这份文档刻意不写密钥的具体路径。** 它在公开仓库里，而恢复所需的私钥位置属于
> 主机机密。需要时在主机上的备份脚本顶部常量里查（`recovery_v3.py` 开头那几个
> `Path(...)` 定义），或用运维记录里的副本。

本手册描述**当前实际在跑的**备份机制与对应的恢复步骤。目录结构与命令均在
2026-11-15 核对过。

---

## 一、有哪些备份

系统同时维护**两套**，用途不同：

| 名称 | 位置 | 形态 | 内容 |
| --- | --- | --- | --- |
| 加密完整快照 | `/root/backups/zhihui-v2/snapshots/<时间戳>/` | `age` 加密分片 | 整套运行环境，可直接还原成一棵树 |
| 每日运行归档 | `/root/backups/workbuddy-daily/recovery-<时间戳>.tar.gz` | 明文 tar.gz | 数据库与运行数据 |

**保留策略**：两套都只留最新的若干份（快照保留 3 份 + 7 天；运行归档只留最新）。
这是 `zhihui-recovery-housekeeping` 与 `zhihui-backup-retention` 两个定时任务做的事
—— **它们只负责清理，不负责备份**。

### 定时任务

| 任务 | 频率 | 作用 |
| --- | --- | --- |
| `zhihui-recovery.timer` | 每天 04:50 左右 | 生成加密完整快照 |
| `zhihui-recovery-housekeeping.timer` | 每天 | 清理旧快照（保留 3 份 + 7 天） |
| `zhihui-backup-retention.timer` | 每天 | 清理旧的运行归档（只留最新） |

查它们是否正常：

```bash
systemctl list-timers --all | grep -E 'zhihui-(recovery|backup)'
systemctl status zhihui-recovery.service --no-pager | tail -5
journalctl -u zhihui-recovery.service --since '-2 days' -o cat | tail -5
```

**健康的样子**：`status=0/SUCCESS`，日志里有 `phase: complete`，耗时十几分钟
（13 分钟左右属正常）。

### 2026-11-15 核对结果

| 项 | 实际 |
| --- | --- |
| `zhihui-recovery.service` 最近一次 | `status=0/SUCCESS`，耗时 13 分 27 秒 |
| 最新快照 | `20261114-043512`，844 MB，21 个分片 |
| 最新运行归档 | `recovery-20261114-053001.tar.gz`，303 MB |
| `age` / `age-keygen` | **已安装**（`/usr/bin/`） |
| 私钥权限 | `-rw------- root root`（符合要求） |
| 私钥对应公钥 | **与 pinned 值一致** |
| 抽查首个分片 SHA-256 | **与清单完全匹配** |

也就是说：**备份在跑、能校验、密钥可用**。缺的只是把步骤写下来——就是本文。

---

## 二、先确认哪一份是好的

### 运行归档

`/root/backups/workbuddy-daily/latest.json` 记录了最新那份的全部校验信息：

| 字段 | 含义 |
| --- | --- |
| `sha256` / `bytes` | 归档本身的校验和与大小 |
| `request_rows` | 归档时的请求行数（可与恢复后对照） |
| `restore_check` | **建包时做过恢复验证**，应为 `ok` |
| `paired_ledger_check.restore_check` | 账本配对文件是否也验证通过 |
| `release_check.signature_verified` | 发行版签名是否验证通过 |

**只要 `restore_check` 不是 `ok`，就不要用它恢复。**

### 加密快照

`<快照目录>/snapshot.json`：

| 字段 | 含义 |
| --- | --- |
| `encryption` | 应为 `age-X25519` |
| `parts[]` | 每个分片的 `name` / `bytes` / `sha256` |
| `cipher_sha256` | 拼接后密文的校验和 |
| `archive_sha256` | **解密后**归档的校验和 |

---

## 三、恢复加密快照

### 3.1 前提

| 项 | 要求 |
| --- | --- |
| 解密私钥 | 主机上的 age 身份文件（位置见备份脚本开头的常量定义） |
| 权限要求 | `0600`、属主 root（**代码会强校验，不满足直接失败**） |
| 对应公钥 | `age15ys9cn70x4gjlc2mjacw7z4dlrn35m8nt5nvavgqnftvvdkrjyxsg9m830` |
| 需要工具 | `age` / `age-keygen` |

> 公钥写在这里是有意的：它能用来**核对**私钥有没有被换过，而知道公钥推不出私钥。

**先确认私钥没被换过**：

```bash
# $IDENTITY 换成脚本里那个私钥路径
ls -l "$IDENTITY"                  # 应为 -rw------- root root
age-keygen -y "$IDENTITY"
# 输出的公钥必须与上面那串完全一致，不一致就停下
```

**公钥不一致意味着这份私钥解不开现有快照** —— 这时候唯一的选择是用别的备份，
不要试图绕过校验。

### 3.2 校验分片

```bash
SNAP=/root/backups/zhihui-v2/snapshots/<时间戳>
cd "$SNAP"
# 逐个核对 snapshot.json 里 parts[] 的 sha256
python3 - <<'PY'
import hashlib, json, pathlib
snap = pathlib.Path('.').resolve()
manifest = json.loads((snap / 'snapshot.json').read_text())
bad = []
for part in manifest['parts']:
    data = (snap / part['name']).read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != part['sha256'] or len(data) != part['bytes']:
        bad.append(part['name'])
print('parts:', len(manifest['parts']), 'bad:', bad or 'none')
PY
```

### 3.3 拼接并解密

```bash
cd "$SNAP"
cat $(python3 -c "import json;print(' '.join(p['name'] for p in json.load(open('snapshot.json'))['parts']))") \
  | age -d -i "$IDENTITY" > /root/recovered.tar.gz

# 核对解密后的校验和
sha256sum /root/recovered.tar.gz
# 必须等于 snapshot.json 里的 archive_sha256
```

### 3.4 解包到一个**新目录**，不要覆盖生产

```bash
mkdir -p /root/recovered && tar -xzf /root/recovered.tar.gz -C /root/recovered
```

**不要直接解到 `/opt/workbuddy-manager`** —— 先解开、核对内容，再决定怎么切换。
直接覆盖会让「恢复失败」变成「两边都没了」。

---

## 四、恢复运行归档

```bash
cd /root/backups/workbuddy-daily
sha256sum recovery-<时间戳>.tar.gz        # 与 latest.json 的 sha256 对照
mkdir -p /root/recovered-daily
tar -xzf recovery-<时间戳>.tar.gz -C /root/recovered-daily
```

解出来的数据库可以直接用 `sqlite3` 打开核对：

```bash
sqlite3 /root/recovered-daily/<库文件> 'PRAGMA quick_check;'          # 应为 ok
sqlite3 /root/recovered-daily/<库文件> 'SELECT count(*) FROM request_logs;'
# 与 latest.json 的 request_rows 对照，数量级应当一致
```

---

## 五、切换时要注意什么

1. **先停写入**，不要一边跑服务一边替换数据库。走 `deploy/` 下的部署流程，
   不要手工 `cp` 覆盖正在用的库。
2. **保留现场**：把出问题的目录改名而不是删除，恢复不成功时还能回退。
3. **恢复后立刻核对**：`/healthz`、`/readyz`、完整性闭包、以及 `request_rows`
   这类可对照的数字。
4. **磁盘余量**：当前可用约 10 GB，而一份快照解开要 2 GB 以上，解包前先看一眼
   `df -h /root`。

---

## 六、知道但暂未启用的事

| 项 | 状态 |
| --- | --- |
| 异地备份 | **未启用**（`offsite: false`）。代码里保留了发布到远端仓库的模式，但需要单独的运维授权才会执行 |
| 自动恢复 | **没有**。备份脚本只有「生成」与「解密自检」两个模式，恢复按本手册手工进行 |

**异地未启用意味着**：本机磁盘损坏会同时丢失生产数据与全部本地备份。
这是已知且被显式接受的风险，不是遗漏。

---

## 七、定期自查

每月抽一次，确认备份**真的能解开**（只做解密与校验，不覆盖生产）：

```bash
# 1. 定时器在跑吗
systemctl list-timers --all | grep zhihui-recovery

# 2. 最近一次成功了吗
journalctl -u zhihui-recovery.service --since '-2 days' -o cat | grep -c 'phase'

# 3. 校验和还对吗（按 §3.2 的方式抽查最新快照）
# 4. 磁盘还够吗
df -h /root
```

**第 3 步比前两步重要**：定时器正常、日志漂亮，但分片损坏的情况是存在的 ——
只有真的算一遍校验和才知道。
