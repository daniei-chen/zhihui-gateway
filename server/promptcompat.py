"""Versioned WorkBuddy system/developer compatibility; application data stays exact."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path


_bundle = json.loads(Path(__file__).with_name('workbuddy-compat.json').read_text(encoding='utf-8'))
if (_bundle.get('schema') != 1 or _bundle.get('platform') != 'workbuddy'
        or _bundle.get('roles') != ['system', 'developer']):
    raise ValueError('unsupported compatibility bundle')
VERSION = _bundle['version']
BUILTINS = tuple((rule['id'], rule['match'], rule['replacement']) for rule in _bundle['rules'])
if (len(BUILTINS) > 64 or len({r[0] for r in BUILTINS}) != len(BUILTINS)
        or any(not 0 < len(pat) <= 4096 or len(rep) > 8192 or pat == rep
               for _, pat, rep in BUILTINS)):
    raise ValueError('invalid compatibility rules')

_matchers = {}
for _rule in _bundle['rules']:
    _variants = _rule.get('variants', [])
    if (not isinstance(_variants, list) or len(_variants) > 16
            or any(not isinstance(value, str) or not 0 < len(value) <= 4096 for value in _variants)
            or not isinstance(_rule.get('case_insensitive', False), bool)):
        raise ValueError('invalid compatibility variants')
    if _variants or _rule.get('case_insensitive'):
        _literals = sorted(set([_rule['match'], *_variants]), key=len, reverse=True)
        _matchers[_rule['id']] = re.compile('|'.join(re.escape(value) for value in _literals),
                                           re.IGNORECASE if _rule.get('case_insensitive') else 0)


@dataclass(frozen=True)
class Applied:
    version: str | None = None
    rules: tuple[str, ...] = ()
    changes: int = 0


def apply(body, *, platform, get_setting):
    if platform != 'workbuddy' or not isinstance(body, dict) or not isinstance(body.get('messages'), list):
        return Applied()
    if get_setting('prompt_compat_enabled') is False:
        return Applied()
    custom = get_setting('prompt_defuse_rules') or []
    rules, patterns = [], set()
    if isinstance(custom, list):
        # Invalid/no-op entries must not claim a signed rule's source pattern.
        for pair in custom[:64]:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or not all(isinstance(x, str) for x in pair)
                    or not 0 < len(pair[0]) <= 4096 or len(pair[1]) > 8192
                    or pair[0] == pair[1] or pair[0] in patterns):
                continue
            pat, rep = pair
            known = next((name for name, p, r in BUILTINS if p == pat and r == rep), None)
            name = known or 'custom-' + hashlib.sha256((pat + '\0' + rep).encode()).hexdigest()[:12]
            rules.append((name, pat, rep))
            patterns.add(pat)
    rules.extend(rule for rule in BUILTINS if rule[1] not in patterns)
    matched, changes = set(), 0

    def fix(text):
        nonlocal changes
        for name, pat, rep in rules:
            matcher = _matchers.get(name)
            updated = matcher.sub(lambda match: rep, text) if matcher else text.replace(pat, rep)
            if updated != text:
                text = updated
                matched.add(name)
                changes += 1
        return text

    for msg in body['messages']:
        if not isinstance(msg, dict) or msg.get('role') not in ('system', 'developer'):
            continue
        content = msg.get('content')
        if isinstance(content, str):
            msg['content'] = fix(content)
        elif isinstance(content, list):
            for part in content:
                if (isinstance(part, dict) and part.get('type') in ('text', 'input_text')
                        and isinstance(part.get('text'), str)):
                    part['text'] = fix(part['text'])
    return Applied(VERSION, tuple(sorted(matched)), changes)
