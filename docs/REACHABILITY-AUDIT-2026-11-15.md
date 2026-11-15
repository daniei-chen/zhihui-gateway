# 可达性盘查 — 2026-11-15

审计前提：**先知道哪些代码真的在跑**。

不知道这件事的代价是双向的：把死代码当攻击面去查，是浪费；
**把死代码当已修，则是幻觉** —— 上两版修的两个缺陷都在不可达模块里。

---

## 一、qtgcore 的实际情况

`main.py` 启动时会打一行 `qoder/trae control plane merged in (qtgcore started)`，
很容易被读成「整个 qtgcore 都接进来了」。**不是。**

实际接入的只有：

| 部分 | 状态 |
| --- | --- |
| `Runtime`（配置与存储） | **活** |
| `checkin_scheduler`（每日签到） | **活** |
| `config.load_settings` | **活** |
| `identity.Identity` | **活** |
| **`qtgcore/routers/compat*.py`（5 个）** | **活**（挂在 `/__qtg` 前缀下） |
| **`qtgcore/routers/proxy.py`** | **不可达** |
| **`qtgcore/authz.py`** | **不可达** |

### 1.1 那些 compat 路由是怎么活的

`main.py` 里：

```python
for _qr in (_qcf, _qst, _qcs, _qace, _qc):
    app.include_router(_qr.router, prefix=_QTG_INTERNAL)
```

`_QTG_INTERNAL = "/__qtg"`，而 `compat.py` 自己的前缀是 `/api` ——
所以真实路由是 `/__qtg/api/...`。

**但这不代表能直接访问。**

### 1.2 直接访问内部前缀会被挡掉

```python
if path.startswith(_QTG_INTERNAL) and not getattr(request.state, "qtg_rewritten", False):
    return JSONResponse({'detail': 'not found'}, status_code=404)
```

**直接打 `/__qtg/api/...` 一律 404。** 这是刻意的防绕过：只有经过中间件、
带 `x-qtg-platform` 头触发**路径重写**的请求才走得通。

实测确认：

| 请求 | 结果 |
| --- | --- |
| `GET /__qtg/api/v1/authz/probe` | **404**（被挡） |
| `GET /__qtg/api/settings` | **404**（被挡） |
| `GET /api/stats/summary`（带 `x-qtg-platform: qoder`） | **401**（活路由，要认证） |

---

## 二、两个不可达模块

### 2.1 `qtgcore/routers/proxy.py`（443 行）

- `@router.` **零匹配** —— 它一个路由都没注册
- 它的鉴权函数 `_auth_key` **零引用**
- 它的 `router` **从未被 include**

`main.py` 的注释已经写明这一点：

> `proxy.py` **未删**：路由同样不可达，但 `compat.py` 依赖其中的
> `_base` / `_catalog` / `_upstream_key`（函数级引用）。

**所以它作为「路由模块」是死的，作为「函数库」是活的。**
上一版修的 `_client_ip` 就在这个文件里 —— 修得对（函数库会被调用），
但当时说的「当前不可达」要更精确：**路由不可达，函数可达**。

### 2.2 `qtgcore/authz.py`

- `decide()` **零调用者**
- 整个 `authz` 模块**没有被任何文件 import**
- `compat*.py` 也不用它

**结论：完全不可达。**

它里面有 44 条路由规则和一套 `_scope_allows` 判定，看起来很重要 ——
**但一次都没执行过**。

---

## 三、这一轮为什么值得记下来

### 3.1 排除了一个看起来像漏洞的东西

`authz._scope_allows` 里有一条：

```python
return (not path.startswith("/api/v1/actions/")
        and (rule is None or ROLE_VIEWER in rule.allow))
```

`rule is None` 时**直接放行** —— 这是 **fail-open 形态**：
新加的路径若没登记规则，只读令牌就能访问。

**看起来是个真问题。但整个模块没被 import，所以它不是。**

如果我只看这段代码就报「发现 fail-open」，那是**假警报**；
如果将来的某次重构把这个模块接上去，它**立刻变成真问题**。

**两种判断都依赖同一件事：知道它可不可达。**

### 3.2 上两版修的东西也需要同样的注脚

| 版本 | 修的东西 | 精确状态 |
| --- | --- | --- |
| v1.2.108 | 身份密钥读取 | **可达**（nginx auth_request 用它） |
| v1.2.109 | 凭据读取 | **可达**（令牌与密钥每请求都读） |
| v1.2.110 | 数据面 IP 提取 | **路由不可达，函数可达** |

**前两个是真修复，第三个是预防。**

---

## 四、还剩什么没盘

这份记录只盘了 `qtgcore`。**`server/` 顶层与 `routers/` 下的可达性没有系统盘过** ——
它们看起来都由 `main.py` 显式 `include_router`（27 处），但没有逐个核对每个 router
的方法是否真的被注册。

**下次要盘的话，正确做法是从 `app.routes` 导出真实路由表，与源码里声明的对比** ——
而不是像我这次一样靠 grep 和 curl 逐个试。
