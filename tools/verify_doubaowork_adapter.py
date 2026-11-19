"""端到端验证：用真实 doubaowork2api 上游测 DoubaoWorkAdapter 的全部契约。

不启动整个 qtg（它的默认配置是 Linux 路径），只验证适配器这一层的
请求/解析/错误处理是否与上游真实响应吻合。

用法：
    DOUBAOWORK_ADMIN_PASSWORD_FILE=/path/to/password \\
      python tools/verify_doubaowork_adapter.py

前置：doubaowork2api 在 127.0.0.1:18789 运行，且密码文件内容与它一致。
**CI 不跑**（没有上游），它是手工验收脚本。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.qtgcore.adapters.doubaowork import DoubaoWorkAdapter  # noqa: E402
from server.qtgcore.config import Settings  # noqa: E402

# 上游管理密码文件。默认取部署里的标准位置；也可用环境变量覆盖。
# **CI 里没有这个文件也没有上游**，所以本脚本不进自动回归，只做手工验收。
PASSWORD_FILE = Path(os.environ.get(
    "DOUBAOWORK_ADMIN_PASSWORD_FILE",
    "/opt/doubaowork2api/secrets/admin_password",
))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


async def main() -> int:
    settings = Settings(
        doubaowork_base="http://127.0.0.1:18789",
        doubaowork_admin_password_file=str(PASSWORD_FILE),
        doubaowork_engine="on",
    )
    adapter = DoubaoWorkAdapter(settings)
    await adapter.start()
    try:
        health = await adapter.health()
        check("health.up", health.get("up") is True, str(health))
        check("health.engine", health.get("engine") == "doubaowork", str(health.get("engine")))

        overview = await adapter.overview()
        check("overview.accounts 是 list", isinstance(overview.get("accounts"), list),
              f"n={len(overview.get('accounts') or [])}")
        check("overview.checkin 为空（上游无签到）", overview.get("checkin") == [])

        pool = await adapter.diagnostic_pool()
        check("diagnostic_pool.scope", pool.get("scope") == "engine_account_list", str(pool.get("scope")))

        models = await adapter.models()
        ids = [m.get("id") for m in (models.get("data") or [])]
        check("models 非空且来自上游 /admin/config", len(ids) > 0, f"n={len(ids)} first={ids[:3]}")

        cfg = await adapter.settings()
        raw = cfg.get("raw") or {}
        check("settings.raw.version == 0.3.0", raw.get("version") == "0.3.0", str(raw.get("version")))
        check("settings.scheduler.enabled is False", cfg["scheduler"]["enabled"] is False)

        keys = await adapter.keys()
        check("keys.data.keys 是 list", isinstance((keys.get("data") or {}).get("keys"), list))

        logs = await adapter.logs()
        check("logs.data 是 list", isinstance(logs.get("data"), list))

        usage = await adapter.usage(14)
        check("usage.usage 空且标记 unsupported",
              usage.get("usage") == [] and "usage" in (usage.get("unsupported") or []),
              str(usage.get("note"))[:60])

        checkin = await adapter.checkin()
        check("checkin 明确不支持，不伪装成功",
              checkin.get("ok") is False and checkin.get("unsupported") is True,
              str(checkin.get("results"))[:80])

        try:
            bad = await adapter.set_account("does-not-exist", True)
            check("set_account 对不存在的 id 不报成功", bad.get("ok") is not True, str(bad)[:100])
        except Exception as exc:  # 上游 404 → UpstreamError，本身就是明确的失败信号
            check("set_account 对不存在的 id 不报成功", True, f"上游明确报错: {str(exc)[:80]}")

        try:
            await adapter.set_account("../admin/password", True)
            check("set_account 拒绝路径注入", False, "未抛异常")
        except ValueError as exc:
            check("set_account 拒绝路径注入", True, str(exc)[:60])

        try:
            await adapter.add_account("x", "not-json")
            check("add_account 拒绝非法 JSON", False, "未抛异常")
        except ValueError as exc:
            check("add_account 拒绝非法 JSON", True, str(exc)[:50])

        saved = await adapter.save_keys([])
        check("save_keys 明确交给上游面板", saved.get("ok") is False, str(saved.get("error"))[:60])
    finally:
        await adapter.close()

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 60)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} :: {detail}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
