"""Canonical portable config validator; generated into each independent service.

Edit here, then run tools/build_config_contracts.py. No app, DB or network imports.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('Configuration rejected: ' + '; '.join(
            item['name'] + ': ' + item['code'] for item in errors))


def load_schema():
    return json.loads(Path(__file__).with_name('config-schema.json').read_text(encoding='utf-8'))


def _parse(raw, spec):
    kind = spec['type']
    value = str(raw).strip()
    if kind == 'integer':
        if not re.fullmatch(r'[+-]?[0-9]+', value):
            raise ValueError('expected_integer_in_' + spec.get('unit', 'count'))
        result = int(value)
    elif kind == 'number':
        try: result = float(value)
        except ValueError: raise ValueError('expected_number_in_' + spec.get('unit', 'count')) from None
        if not math.isfinite(result): raise ValueError('expected_finite_number')
    elif kind == 'boolean':
        accepted = {'0','1','true','false','yes','no','on','off'}
        if spec.get('boolean_dialect') != 'manager':accepted |= {'y','n','t','f'}
        if value.lower() not in accepted:
            raise ValueError('expected_boolean')
        return value.lower() in {'1','true','yes','on','y','t'}
    elif kind == 'absolute_path':
        if not PurePosixPath(value).is_absolute() and not PureWindowsPath(value).is_absolute():
            raise ValueError('expected_absolute_path')
        return value
    else:
        result = value
    if spec.get('format') in ('http_url','proxy_url'):
        try:
            url = urlsplit(value)
            allowed = {'http','https'} if spec['format']=='http_url' else {'http','https','socks4','socks5','socks5h'}
            if url.scheme not in allowed or not url.hostname or url.fragment:raise ValueError
            if url.port is not None and not 0 < url.port <= 65535:raise ValueError
        except ValueError:raise ValueError('invalid_url') from None
    if spec.get('format') == 'ip_network_list':
        try:
            for item in value.split(','):ipaddress.ip_network(item.strip(),strict=False)
        except ValueError:raise ValueError('invalid_ip_network') from None
    if spec.get('format') == 'iana_timezone':
        try: ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):raise ValueError('invalid_timezone') from None
    if 'choices' in spec and str(result).lower() not in spec['choices']:
        raise ValueError('invalid_choice')
    if 'normalize_minimum' in spec:
        result = max(spec['normalize_minimum'], result)
    if 'minimum' in spec and result < spec['minimum']: raise ValueError('below_minimum')
    if 'maximum' in spec and result > spec['maximum']: raise ValueError('above_maximum')
    if spec.get('exclusive_minimum') is not None and result <= spec['exclusive_minimum']:
        raise ValueError('below_exclusive_minimum')
    return result


def _safe(value, spec):
    if spec.get('sensitive'):
        return {'redacted': True, 'value_set': value not in (None, '')}
    if isinstance(value, str) and '://' in value:
        try:
            parsed = urlsplit(value)
            # All URL paths, queries and userinfo can contain credentials.
            host = parsed.hostname or ''
            if ':' in host: host = '[' + host + ']'
            return parsed.scheme + '://' + host + (':' + str(parsed.port) if parsed.port else '')
        except ValueError:
            return '<url-redacted>'
    return value


def validate_environment(environment=None, *, schema=None):
    schema = schema or load_schema()
    environment = dict(os.environ if environment is None else environment)
    specs, errors = schema['settings'], []
    for name in sorted(environment):
        if any(name.startswith(prefix) for prefix in schema['prefixes']) and name not in specs:
            errors.append({'name': name, 'code': 'unknown_variable'})
    rows = {}
    for name, spec in specs.items():
        raw = environment.get(name)
        present = raw is not None and bool(str(raw).strip())
        value = raw if present else spec.get('default')
        try:
            parsed = _parse(value, spec) if value is not None and str(value).strip() else value
        except (ValueError, TypeError) as exc:
            # The value is deliberately absent from diagnostics, including unknown keys.
            errors.append({'name': name, 'code': str(exc)})
            continue
        rows[name] = {'value': _safe(parsed, spec),
                      'source': 'process_environment' if present else (
                          'derived_default' if 'default' not in spec else 'default'),
                      'type': spec['type'], 'unit': spec.get('unit'),
                      'consumers': spec.get('consumers', [])}
        if 'normalize_minimum' in spec:
            rows[name]['normalization'] = 'legacy_minimum_clamp'
            rows[name]['normalization_applied'] = bool(present and int(str(raw).strip()) != parsed)
            if present and not spec.get('sensitive'):
                rows[name]['requested_value'] = int(str(raw).strip())
    if errors: raise ConfigurationError(errors)
    if schema['service']=='manager':
        graceful=rows.get('WB_GRACEFUL_SHUTDOWN_S',{}).get('value')
        drain=rows.get('WB_DRAIN_TIMEOUT_SEC',{}).get('value')
        if isinstance(graceful,(int,float)) and isinstance(drain,(int,float)) and graceful < drain:
            raise ConfigurationError([{'name':'WB_GRACEFUL_SHUTDOWN_S','code':'less_than_drain_timeout'}])
    return {'schema':schema['schema'], 'service':schema['service'],
            'schema_sha256':hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest(),
            'settings':rows,
            'source_note':'Process environment is already resolved by the launcher; unit file provenance must be collected separately.'}


def with_effective_values(snapshot, values, *, source):
    """Overlay resolved settings without exposing credentials or guessing provenance."""
    result=json.loads(json.dumps(snapshot))
    specs=load_schema()['settings']
    for name,value in values.items():
        if name not in specs:continue
        result['settings'][name]['environment_value']=result['settings'][name]['value']
        result['settings'][name]['value']=_safe(value,specs[name])
        result['settings'][name]['source']=source
    return result


def main():
    parser = argparse.ArgumentParser(description='Validate config without importing app code or opening data files')
    parser.add_argument('--env-json', type=Path, help='Optional JSON environment object for offline preflight')
    args = parser.parse_args()
    try:
        env = json.loads(args.env_json.read_text(encoding='utf-8')) if args.env_json else None
        if env is not None and (not isinstance(env, dict) or any(not isinstance(v,str) for v in env.values())):
            raise ValueError('Environment input must map names to strings')
        print(json.dumps({'ok':True, 'configuration':validate_environment(env)},ensure_ascii=False,indent=2))
        return 0
    except ConfigurationError as exc:
        print(json.dumps({'ok':False,'errors':exc.errors},ensure_ascii=False))
        return 2
    except (ValueError, OSError):
        print(json.dumps({'ok':False,'errors':[{'code':'invalid_preflight_input'}]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
