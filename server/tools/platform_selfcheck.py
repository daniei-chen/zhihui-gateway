#!/usr/bin/env python3
"""平台属性自洽 + 判定等价自检。

2026-11-22 把「有哪些平台」从 12 处硬编码元组收敛到 `platformpolicy.PLATFORM_SPEC`
时，用这个脚本验收：**对已有平台的判定结果必须一个字都没变**。

新增平台时：把它加进 `PLAT`，并在 `OLD` 里写清「收敛前的字面量判定」、
在 `NEW` 里写「现在的查表判定」，然后跑一次 —— 差异必须为 0。

用法（在仓库根目录执行）：
    PYTHONPATH=. python3 server/tools/platform_selfcheck.py
退出码 0 = 全部等价；1 = 有不一致，输出会指出是哪一项哪个平台。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys


def _load_policy():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "platformpolicy.py"
    spec = importlib.util.spec_from_file_location("platformpolicy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["platformpolicy"] = module      # dataclass 需要模块已注册
    spec.loader.exec_module(module)
    return module


pp = _load_policy()

#: 覆盖在营 + 历史 + 未知。
PLAT = ("workbuddy", "trae", "doubaowork", "qoder", "unknown")

#: 收敛前各处的字面量判定（这里就是「原来的行为」）。
OLD = {
    "passthrough": ("qoder", "trae", "doubaowork"),
    "stats_scope": ("qoder", "trae"),
    "oauth_session": ("qoder", "trae"),
    "realm_skip": ("qoder", "trae", "doubaowork"),
    "poll_mode": ("qoder",),
    "adapter": {"qoder": "qoder", "trae": "trae", "workbuddy": "wb", "doubaowork": "dbw"},
}

#: 现在的查表判定。
NEW = {
    "passthrough": pp.is_passthrough,
    "stats_scope": lambda n: bool(pp.spec(n) and pp.spec(n).stats_scope),
    "oauth_session": lambda n: bool(pp.spec(n) and pp.spec(n).oauth_session),
    "realm_skip": lambda n: not (pp.spec(n) is None or pp.spec(n).realm_scoped),
    "poll_mode": lambda n: bool(pp.spec(n) and pp.spec(n).auth_mode == "poll"),
    "adapter": pp.adapter_attr,
}

EXPECT = {
    "PLATFORMS set": set(pp.PLATFORMS) == {"workbuddy", "trae", "doubaowork"},
    "_ACCEPTED set": set(pp._ACCEPTED) == {"workbuddy", "trae", "doubaowork", "qoder"},
    "DEFAULT_PLATFORMS": set(pp.DEFAULT_PLATFORMS) == {"workbuddy", "trae", "doubaowork"},
    "passthrough_names": set(pp.passthrough_names()) == {"trae", "doubaowork", "qoder"},
}


def main() -> int:
    bad = 0
    for key, old_value in OLD.items():
        for name in PLAT:
            old = old_value.get(name, "") if key == "adapter" else name in old_value
            new = NEW[key](name)
            if old != new:
                bad += 1
                print("DIFF %-15s %-11s old=%r new=%r" % (key, name, old, new))
        print("%-15s 等价（%d 个平台名）" % (key, len(PLAT)))
    print()
    for key, ok in EXPECT.items():
        print("%-18s %s" % (key, ok))
        if not ok:
            bad += 1
    print()
    print("不一致项: %d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
