import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qtg' / 'app'))
from qtg import routeaudit  # noqa: E402

out = ROOT / 'qtg' / 'tests' / 'data' / 'route_regression.json'
inv = routeaudit.inventory()
golden = {
    "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
    "note": "由 routeaudit.inventory() 生成；改动路由后重跑 tools/regen_route_golden.py 并人工 review 重复组。",
    "declarations": inv["declarations"],
    "unique_method_paths": inv["unique_method_paths"],
    "duplicate_groups": inv["duplicate_groups"],
    "duplicate_extra_declarations": inv["duplicate_extra_declarations"],
    "duplicate_keys": sorted(f"{g['method']} {g['path']}" for g in inv["duplicates"]),
    "effective_digest": inv["effective_digest"],
    "effective_digest_schema": inv['effective_digest_schema'],
    "effective_handlers": [{k: row[k] for k in ('method', 'full_path', 'module', 'func')}
                           for row in inv['effective']],
    "registration_order": inv["registration_order"],
}
out.write_text(json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8")
(ROOT / 'design' / 'qtg' / 'route-table.json').write_text(
    json.dumps(inv, ensure_ascii=False, indent=2), encoding='utf-8')
print("golden written:", out, "dups:", len(golden["duplicate_keys"]))
