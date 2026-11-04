import argparse
import importlib.util
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline Zhihui contract-v1 compatibility check; not a general OpenAPI differ')
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    options = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1] / 'server/protocolcontracts.py'
    spec = importlib.util.spec_from_file_location('zhihui_protocolcontracts', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result = module.compare(json.loads(options.before.read_text(encoding='utf-8')),
                                json.loads(options.after.read_text(encoding='utf-8')))
    except (ValueError, OSError, TypeError, KeyError):
        print(json.dumps({'ok': False, 'error': 'invalid_contract'}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result['breaking'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
