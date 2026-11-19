# qoder 摘除 + 豆包工作顶替（2026-11-19）

## 一句话

qoder 官方不再送积分，对「免费额度聚合」这件事不再有产出。本次把它从
**平台集合、编号契约、前端入口**里摘掉，位置由**豆包工作**接管。

## 改了什么

### 后端：平台集合（三处必须同步）

| 文件 | 原值 | 新值 |
|---|---|---|
| `server/platformpolicy.py` `PLATFORMS` | `('workbuddy','qoder','trae','doubaowork')` | `('doubaowork','trae','workbuddy')` |
| 同上 `DEFAULT_PLATFORMS` | `('workbuddy','qoder','trae')` | `('doubaowork','trae','workbuddy')` |
| `server/qtgcore/routers/compat_fixes.py` `PLATFORMS` | 4 个含 qoder | 3 个不含 |
| `server/qtgcore/routers/compat_stats.py` `PLATFORMS` | 同上 | 同上 |
| `server/qtgcore/routers/proxy.py` `PLATFORMS` | 同上 | 同上 |

**顺序不是随手排的**：数组顺序就是 UI 显示顺序，豆包放第一位（qoder 原来的位置）。

### 后端：编号契约（豆包顶替 1 号）

```python
# 原
UPSTREAM_IDS      = {1: "qoder", 2: "trae", 3: "workbuddy", 4: "doubaowork"}
UPSTREAM_NAMES    = {1: "Qoder", 2: "Trae", 3: "WorkBuddy", 4: "豆包工作"}
_PLAT_TO_UPSTREAM = {"qoder": 1, "trae": 2, "workbuddy": 3, "doubaowork": 4}

# 新
UPSTREAM_IDS      = {1: "doubaowork", 2: "trae", 3: "workbuddy"}
UPSTREAM_NAMES    = {1: "豆包工作", 2: "Trae", 3: "WorkBuddy"}
_PLAT_TO_UPSTREAM = {"doubaowork": 1, "trae": 2, "workbuddy": 3}
```

**为什么是「1 号换主人」而不是「豆包留 4 号」**：外部面板按 `upstream_id`
选平台，前端 dashboard 逐组扇出。留着空洞或让豆包排到 4 号，会让「1 号平台」
这个位置永久缺失，前端多一组永远空的。2/3 号不动，只换 1 号。

### 后端：qoder 变成「可摘除」

`server/qtgcore/runtime.py`：

```python
qoder_engine = (settings.qoder_engine or "off").strip().lower()
if qoder_engine == "off":
    self.qoder = None          # ← 不创建适配器
elif qoder_engine == "qoder2":
    self.qoder = Qoder2Adapter(settings)
else:
    self.qoder = QoderAdapter(settings)
```

`server/qtgcore/config.py`：`qoder_engine` 默认值 `"bff"` → `"off"`，
可选值加 `off`。

**摘除方式是「不创建实例」，不是「删代码」**：`self.qoder = None` 之后，
`build_aggregate` 靠 `need_q = ... and q is not None` 自然跳过，下游拿到的
是「这个平台不存在」，而不是「这个平台一直报错」。

`server/qtgcore/service.py` 的 `build_aggregate` 同步加了 `q is not None`
保护 —— 原来 `q`/`t` 是硬取（假定一定存在），摘掉 qoder 后每一路都会
`AttributeError` 被 `_safe` 记成错误，聚合结果里会挂一串假故障。

**回滚路径**：写 `off` 之外的任何值（`bff` / `qoder2`）就能把它拉回来。

### 前端

| 文件 | 改动 |
|---|---|
| `web/lib/platform-store.ts` | `PLATFORMS` 改成三条。**`Platform` 类型仍保留 qoder**（见下） |
| `web/app/(main)/keys/page.tsx` | `KEY_PLATFORM_OPTIONS` 三选一；`LEGACY_DEFAULT_PLATFORMS` 跟后端一致；`PLATFORM_LABELS` 改成 `Partial<Record<...>>` |

**为什么 `Platform` 类型不删 qoder**：代码里还有 `platform === 'qoder'` 这类
历史分支（统计口径、账号导入对话框）。删类型会引发一大片编译错误，而那些分支
在「UI 永远不产出 qoder」的前提下是**死代码**，留着无害。判断「在不在用」
一律看 `PLATFORMS`，不要看 `Platform` 类型。

`PLATFORM_LABELS` 保留 `qoder: 'Qoder'` 是因为**旧密钥数据里可能还写着
qoder**，展示时回退成名字比露出原始 id 好。

## 修了什么测试（这些原本是「用 qoder 当示例平台」）

| 测试 | 原写法 | 问题 |
|---|---|---|
| `test_doubaowork_platform` 三条 | 断言 `DEFAULT_PLATFORMS == ['workbuddy','qoder','trae']` | 平台集合变了 |
| `test_audit_20261029_gateway_ledger` 十条 | `_THREEWAY_SPEC` patch 只含 `('qoder','trae')` | doubaowork 请求回落到 workbuddy.mock |
| `test_count_tokens_admission` 一条 | `key.platforms=['qoder']` + 无前缀 `model='m'` | **平台判定靠 model 前缀**，无前缀根本不触发；qoder 不在 PLATFORMS 里也不再触发 |

最后那条顺带暴露了一个既有问题：那条测试以前「通过」靠的不是它想测的东西。

## 验证

```
后端回归：[a-f] 1403 tests  11F/19E = 基线（零新增失败）
前端 tsc：0 错误
前端测试：178/178
```

## 没做的事

- **没有删 qoder 的适配器文件**（`adapters/qoder.py` / `qoder2.py`）：它们是
  回滚路径，而且删了两百多处测试引用要一起改，收益不抵风险。
- **没有删 qoder 的配置项**（`qoder_base` / `qoder_password_file` 等）：同理，
  而且 `_abs()` 校验会因为缺字段直接拒绝启动。
- **没有改 VERSION / CHANGELOG**：发版是独立签名流程。

---

## 本地验证结果（2026-11-19）

服务起在 `http://127.0.0.1:8765`（试用数据在 `qtg-e2e/trial-run/`，与生产隔离）。

### 启动日志

```
INFO qtg.runtime: qtg 启动：qoder=off(-) trae=trae(http://127.0.0.1:8792)
                  wb=workbuddy dbw=doubaowork data=...trial-run\var
```

`qoder=off` —— 适配器根本没创建，不是「创建了但不健康」。

### `/api/platforms`

```json
total=3
  doubaowork   upid=1  enabled=True  checkin=True  verified=True
  trae         upid=2  enabled=True  checkin=True
  workbuddy    upid=3  enabled=True  checkin=False
```

**豆包在 1 号位**（qoder 原来的位置），2/3 号不动。

### 其余端点

| 端点 | 结果 | 说明 |
|---|---|---|
| `/api/accounts` | 409 | 上游未启动，正确报「doubaowork 上游不可达」 |
| `/api/checkin-logs` | 200 | 3 条历史，`claimed`/`already`/`failed` 分类正确 |
| `/api/models` | 200 | 空列表 + `source: static` |
| `/api/stats/daily` | 200 | 14 个桶（无流量时如实为空） |
| `/api/logs` | 200 | 空 |

### 测试

```
后端 `[a-f]` 1403 tests   11F/19E = 基线（零新增）
前端 tsc                  0 错误
前端 node --test          178/178
```

## 中途踩到的坑（写下来免得下次重犯）

1. **只改了 `PLATFORMS` 没改 `startup()`** → `self.qoder.start()` 对 `None` 调用
   → 整个 qtgcore 控制面启动失败 → 四个平台管理面全挂。
   **教训：把一个对象改成可为 None，必须扫一遍所有 `.method()` 调用点。**

2. **`health_snapshot()` 把 None 放进待探测列表** → `probe(None)` AttributeError。

3. **一次性把测试里的 `'qoder'` 批量替换成 `'doubaowork'`** → 改坏了 35 个测试。
   **教训：批量替换前先分清「用 qoder 当示例平台」和「测的就是 qoder 平台」，
   后者只能 skip，不能替换。**

4. **`normalize()` 直接用 `PLATFORMS` → 存量密钥里的 qoder 让鉴权 500**。
   改成「对外枚举 `PLATFORMS` / 内部可解析 `_ACCEPTED`」两层，才既不显示
   qoder、又不会让历史数据炸掉。

## 上线前还需要你确认的

1. **国内服务器上的 `qoder_engine` 开关文件**
   （`/opt/workbuddy-manager/engine/var/qoder_engine`）：如果它写着 `bff`，
   环境变量/file 优先级会把它读回来，qoder 就又回来了。**部署后要确认
   这个文件不存在或内容为 `off`。**

2. **qoder 反代服务本身**（`127.0.0.1:8791` / `8799`）：qtg 不再连它，
   但那个进程还在跑。要不要停掉由你决定——它不占用 qtg 的资源。

3. **存量密钥的 `platforms` 字段**：里面可能还写着 `qoder`。程序能识别
   （不会 500），但界面上会显示一个已下线的平台。要不要批量清理，
   是一次性数据操作，不是代码改动。

---

## 最终验证（2026-11-19 收尾）

### 后端回归：与基线零差异

```
[a-f]  1403 tests   11F/19E   ← 基线
[g-m]   899 tests    0F/3E    ← 基线
[n-s]  1627 tests    8F/18E   ← 基线
[t-z]   910 tests    7F/2E    ← 基线
──────────────────────────────
       4839 tests   合计 65

与基线双向差集（Compare-Object）为空。
```

那 65 个是**改造前就已存在**的失败：symlink/umask 权限测试（Windows 上跑不了）、
`test_docs_packaging`（打包白名单）、live HTTP 测试（需要外部 mock 服务器）、
以及 node 前端断言。**没有一个和本次改造有关。**

### 前端

```
tsc --noEmit          0 错误
node --test           243/243 通过（含 lib/ 下 65 条之前没被跑到过的）
```

### 运行时

```
INFO qtg.runtime: qtg 启动：qoder=off(-) trae=trae(http://127.0.0.1:8792)
                  wb=workbuddy dbw=doubaowork

GET /api/platforms  → total=3
  doubaowork   upid=1  enabled=True
  trae         upid=2  enabled=True
  workbuddy    upid=3  enabled=True
```

### 改动规模

```
58 个文件变更，+1324 / -303
13 个新文件（豆包适配器、签到实现、8 个测试、3 个诊断工具、2 份文档）
```

## 交付方式

服务跑在 **http://127.0.0.1:8765**（Windows 本机，试用数据在
`qtg-e2e/trial-run/`，与你的生产数据完全隔离）：

```powershell
cd "D:\AI\DeepSeek\DeepSeek 3\qtg-e2e"
python run_trial.py --port 8765
# 浏览器打开 http://127.0.0.1:8765
# 管理密码：trial-admin-pass
```

**你要重点看的三处**：

1. **顶部平台切换器** —— 应该只有三个 pill：豆包工作 / Trae / WorkBuddy
2. **设置 → 管家面板** —— 四列变三列，豆包在第一位
3. **密钥页新建密钥** —— 平台复选框三个，豆包在第一位

如果这三处对，剩下的（账号页、统计页、签到记录）跟着平台走，逻辑一致。

## 换国内服务器时**必须**确认的两件事

1. **开关文件**：`/opt/workbuddy-manager/engine/var/qoder_engine`
   如果它写着 `bff`，`_file_switch` 会把它读回来 → qoder 又活了。
   **部署后确认它不存在，或内容为 `off`。**

2. **`QTG_QODER_ENGINE` 环境变量**：同理，显式设成 `off` 或者干脆不设
   （默认就是 `off`）。

其余步骤和平时发版一样，没有额外的迁移动作。存量密钥里的 `qoder` 字段
不会让鉴权失败（`_ACCEPTED` 兜住了），只是界面上会显示一个已下线的平台名。
