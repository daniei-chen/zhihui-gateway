# 加密密钥与备份的关系：设计是对的，风险在架构不在代码（第 51 轮）

## 1. 想查什么

备份是加密的。但如果**加密私钥和备份放在同一台机器上**，那「加密」就只防一种情况
（别人拿到备份文件但没有 root），防不了另一种（整机丢失 / 磁盘被拔走）。

这是要查的核心问题。

## 2. 第一层：备份里有没有混进私钥

这是最坏的情况 —— 如果私钥被打包进归档，那么**拿到备份的人同时也拿到了私钥**，
加密等于不存在。

查归档内容：

```
$ tar tzf recovery-20261125-144335.tar.gz | grep -iE 'keys\.txt|\.age/|priv|secret|identity|\.pem'
manager/fork-tools/mint_identity_test.py
manager/fork-tools/apply_nginx_identity.py
manager/engines/tests/_test_risk_identity.py
manager/server/edgeidentity.py
...
```

**匹配到的全是源码文件名里带 "identity" 字样，没有一个是真的密钥材料。**

再看归档里的私有材料目录 `private-host/`（5 个文件）：

```
private-host/workbuddy-web.unit.txt
private-host/workbuddy-web.properties.txt
private-host/gateway.image.json
private-host/python-runtime.json
private-host/environment-references.json
```

其中唯一可能藏密钥的是 `environment-references.json`。它的完整内容是：

```json
{"paths": []}
```

**空的。**而且文件名就叫「**references**」（引用）不是「values」（值）。

**结论：归档里没有任何密钥材料。加密没有被自己的备份抵消。**

## 3. 第二层：架构层面的风险（真实存在，但改不了）

密钥和备份的实际位置：

| 东西 | 位置 |
|---|---|
| age 加密私钥 | `/root/.config/age/keys.txt` |
| age 公钥 | `age15ys9cn70x4gjlc2mjacw7z4dlrn35m8nt5nvavgqnftvvdkrjyxsg9m830` |
| 加密备份 | `/root/backups/workbuddy-daily/` |

**同一台机器。**

所以：

| 场景 | 加密有没有用 |
|---|---|
| 有人拿到了备份文件，但没有 root | **有用** |
| 整机丢失 / 磁盘被拔走 | **没用**（密钥和密文一起丢了） |

而且代码里这个选择是**显式**的：

```python
'offsite': False
```

以及一条保护性拒绝：

```python
if Path('/root/backups/workbuddy-recovery/offsite.json').exists():
    raise RuntimeError('legacy offsite publishing is active; review before using the v2 path')
```

**说明作者知道这件事，并且主动选择了「只做本地」。** 这是有意的设计取舍，不是遗漏。

## 4. 为什么这一轮不改代码

异地备份需要**外部存储 + 凭据**，这是运维决策，不是代码问题。硬在代码里加一个「上传到某处」
的开关，只会得到一个没有目标地址的空开关。

但有一件事**现在就能做，而且不需要任何外部依赖**：

**把 age 公钥抄下来，离线保存。**

因为 age 是公钥加密：**公钥可以公开，私钥不能丢**。它们的实际用途不同：

| 材料 | 用途 | 丢失后果 |
|---|---|---|
| **公钥**（可以公开） | 加密新备份 | 无（可从私钥重新导出） |
| **私钥**（必须保管） | 解密历史备份 | **所有历史备份永久无法恢复** |

**所以真正该离线保管的是 `/root/.config/age/keys.txt`，不是公钥。**

如果运维能把这一份私钥拷到机器之外（密码管理器、离线 U 盘、纸质备份都行），
那么「整机丢失」这个场景就从「所有备份作废」变成「只要还能拿到机器外的私钥，
就能在任意一台新机器上解密历史备份」。

**这是一次拷贝就能消除的风险。**

## 5. 建议的运维动作（需要人做，我做不了）

```
1. 以 root 读取 /root/.config/age/keys.txt
2. 确认首行是 `# public key: age15ys9cn70x4gjlc2mjacw7z4dlrn35m8nt5nvavgqnftvvdkrjyxsg9m830`
   （用它核对拿到的私钥和这台机器上的公钥是配对的）
3. 把这份私钥存到机器之外
4. 存好之后，随便取一个历史备份做一次解密验证：
   age -d -i <离线私钥> recovery-YYYYMMDD-HHMMSS.tar.gz.age > /dev/null
```

第 4 步是**必须做**的 —— 没有验证过的备份恢复流程，和没有备份区别不大。

## 6. 这一轮的产出

| 项 | 结论 |
|---|---|
| 归档是否泄漏私钥 | **没有** |
| `private-host/` 是否含密钥值 | **没有**（只有空引用） |
| 加密是否被备份自身抵消 | **没有** |
| 同机风险 | **存在，且是显式设计取舍** |
| 能否用代码改善 | **不能**（需要外部存储） |
| 能立刻做的事 | **把 age 私钥离线保管并验证解密** |

**这一轮没有改代码。**按规范 §2.1，「找不到值得做的，就做诊断和验证，把结论写清楚」——
这一轮属于诊断，结论是「代码侧无需改动，风险在一次人工拷贝」。

## 7. 待办

| 项 | 归属 |
|---|---|
| **age 私钥离线保管 + 解密验证** | **运维（一次拷贝）** |
| 异地备份 | 运维（需外部存储） |
| 完整恢复演练 | 需专门时间窗 |