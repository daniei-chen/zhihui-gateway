# 测试相关源码修改建议（PROPOSALS）

> 硬约束：本轮只写 `tests/` 与 `evidence/`，**不改** `manager/`、`qtg/` 业务源码。
> 下面是“为了能验证/为了消除噪声，业务源码或测试源码该怎么改”的建议，均**未实施**。
> 每条都附带证据位置与影响面，供后续授权批次决定。

## P1（建议优先）：QTG CSRF 中间件调用了不存在的方法

- 位置：`qtg/app/qtg/main.py` 的 `_csrf_gate` 中间件
  ```python
  rt.credentials.check_origin(request)   # ← 现在这行
  ```
- 事实：`check_origin` 定义在 `qtg/app/qtg/security.py` 的 **`Auth` 类**（`class Auth`，
  第 181 行起），而 `rt.credentials` 是 **`CredentialStore`**（第 33 行起）。运行时
  `hasattr(CredentialStore, 'check_origin') == False`（已实测）。
- 后果：`_csrf_gate` 捕获 `AttributeError` 后一律返回
  `403 {"code":"csrf_forbidden","detail":"'CredentialStore' object has no attribute 'check_origin'"}`。
  所有**非 Bearer** 的 `POST/PUT/PATCH/DELETE /api/**` 请求都被 403 拦下
  （Cookie 会话写操作完全不可用），未鉴权请求的错误码也从 401 变成 403。
- 证据：`evidence/junit-qtg.xml` 中
  `tests.test_api.ActionsAuthTest::test_all_writes_require_auth`
  （`403 != 401`）与 `test_auth_precedes_body_validation`
  （响应体里直接回显了 AttributeError 文本）。
- 建议修复：改为 `rt.auth.check_origin(request)`，并补一条“Cookie 会话跨站写必须 403、
  同站写必须放行”的测试；修复前不要放宽测试期望值。
- 关联审计条目：身份/授权/CSRF 相关条目（审计台账里的 K18 一类；请 Lead 按最新编号核对）。

## P2：QTG 配置默认值写死 Linux 绝对路径

- 位置：`qtg/app/qtg/config.py` 的 `Settings` 字段默认值（`/opt/...`）+ `_abs()`。
- 事实：`_abs()` 用 `Path(v).is_absolute()` 校验；在 Windows 上 `Path('/opt/x')` 不是
  绝对路径 → `ValueError: QTG_QODER_PASSWORD_FILE 必须是绝对路径`。
  这会让 `test_core.ConfigTest.test_derived_paths` /
  `test_env_override_and_bool_parsing` 在本机必然失败（Linux 上通过）。
- 建议：`_abs()` 改用 `os.path.isabs(v)` 并允许“平台默认值”；或者把默认值全部收敛到
  一个 `defaults` 映射，由部署环境注入。**不要**为了让它过而删除绝对路径校验
  （安全性来自“必须是绝对路径”，不能降级）。
- 关联：测试可移植性，不是安全缺陷。

## P3：QTG `ConfigTest.tearDown` 清空所有 `QTG_*`，造成跨用例污染

- 位置：`qtg/tests/test_core.py::ConfigTest.tearDown`
  ```python
  for k in list(os.environ):
      if k.startswith("QTG_"):
          os.environ.pop(k, None)
  ```
- 问题：它把**运行器注入的隔离路径**也一并删掉，后续用例回落到 `/opt/...` 默认值；
  在 Linux 上恰好不报错，在 Windows 上直接 ValueError。测试之间也互相污染。
- 建议：改成 `unittest.mock.patch.dict(os.environ, {...}, clear=False)` 上下文，或
  `self.addCleanup` 恢复快照；这样不依赖宿主机路径。
- 关联：K48（fixture 问题先修，再判生产 bug）。

## P4：Manager 测试把“仓库根 + web/”写死，部署副本无法跑

- 事实：`manager/server/tests/*` 大量使用
  `Path(__file__).resolve().parents[2] / 'web' / lib / *.test.mjs` 与
  `parents[2] / 'web' / 'app' / ...`；而部署副本 `manager/` 里没有 `web/` 目录，
  于是产生 **291 个 `FileNotFoundError`**（本次运行实测），把真实失败淹没。
- 建议（择一）：
  1. 发布/测试流水线在跑测试前把前端源码挂到 `manager/web`（本仓库 `src/three-platform/web`），
     并**校验版本一致**（例如对比 `package-lock` 与后端 `CHANGELOG`）；
  2. 测试统一走一个 `tests/paths.py` 的 `WEB_ROOT`（可被 `WB_TEST_WEB_ROOT` 覆盖），
     缺目录时用 `skipIf` 并写清理由——明确跳过，不静默。
- 反对做法：为了让 267 个失败变绿而删断言 / 加 `-k` 过滤（审计明确禁止）。

## P5：`test_upstream_patches.py` 的 `dev` 包依赖

- 事实：`from dev.apply_upstream_patches import apply_patches` 在部署态
  （`/opt/workbuddy-manager/releases/current/`，无 `dev/`）不可能成立；
  在完整源码树里 `src/three-platform/dev/` 存在，测试可跑。
- 本轮处理：**不改测试**，由隔离运行器把 `src/three-platform` 注入 `sys.path`
  （等价于 pytest 的 `pythonpath`），收集错误从 1 → 0，6 个用例全部执行。
- 建议（如需让裸 `pytest` 也可收集）：
  - 在仓库根加 `pytest.ini`：`[pytest] pythonpath = src/three-platform`（pytest ≥7），
    或
  - 测试内加明确的 `pytest.importorskip('dev', reason='需要完整源码树的 dev/ 工具链')`。
  - 两种都比“静默 ignore 该文件”好：前者真跑，后者可追溯。

## P6：`test_update_e2e` / `test_update_package` 在临时目录里构造部署包

- 事实：测试会在 `%TEMP%` 下创建 `workbuddy-manager-v9.9.9/`、`workbuddy2api/` 等
  **模拟**部署目录并写入文件。这是合法测试数据。
- 提示：任何“生产路径守卫”都必须先判断是否在隔离根内，否则会把模拟包误判成生产
  写入（本轮的隔离器初版就踩过，已修正；见 `pytest_plugin._install_path_guard`）。
- 建议：业务侧不需要改；如果后续有别的守卫/审计工具，同样要区分“临时模拟目录”。

## P7：QTG 备份脚本在 Windows 不可执行

- 事实：`test_api.ActionsAuthTest::test_all_writes_accept_token` 走 `/api/v1/actions/backup`
  时 `subprocess` 执行 `/opt/backup/*.sh`，Windows 上 `[WinError 2] 系统找不到指定的文件`。
- 建议：测试里用 `sys.executable` 或平台可用脚本；实现侧不需要为 Windows 改
  （生产是 Linux），但**测试可移植性**可以提升。

## P8：假密钥约定

- 本轮隔离器注入 `OPENAI_API_KEY=sk-fake-for-isolated-tests-only`，并被 `gate.ps1` 的
  敏感值扫描明确放行（`tests/` 目录 + 值里含 `-`，不匹配真实 key 形态）。
- 建议：仓库约定所有测试假密钥统一以 `sk-fake-` / `fake-` 开头，门禁即可白名单化，
  避免把真 key 混进测试夹具。
