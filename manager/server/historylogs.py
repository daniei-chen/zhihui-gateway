"""SQL-first task/check-in paging; realm filtering precedes count and LIMIT.

Legacy logs do not record historical realm. Only unambiguous current identities
can be scoped; deleted or ambiguous identities are explicitly unknown.
"""
from __future__ import annotations
import json
from . import db


def _scope(mapping, realm):
    if mapping is None or not realm:
        return '1', []
    prefix = {}
    for uid, value in mapping.items():
        prefix.setdefault(uid[:8], set()).add(value)
    known = {uid: value for uid, value in mapping.items()}
    for uid, values in prefix.items():
        if len(values) == 1 and uid not in known:
            known[uid] = next(iter(values))
    if realm == 'unknown':
        recognized = [uid for uid, value in known.items() if value in ('cn', 'global')]
        return "COALESCE(uid,'')<>'' AND uid NOT IN (SELECT value FROM json_each(?))", [json.dumps(recognized)]
    allowed = [uid for uid, value in known.items() if value == realm]
    return "(COALESCE(uid,'')='' OR uid IN (SELECT value FROM json_each(?)))", [json.dumps(allowed)]


def task_page(*, mapping, realm, limit, offset, uid, kind, days):
    base, args = db._task_log_where(uid=uid, days=days)
    scope, values = _scope(mapping, realm)
    where = (base + ' AND ' if base else ' WHERE ') + '(' + scope + ')'
    params = [*args, *values]
    selected = where + (' AND kind=?' if kind else '')
    selected_args = [*params, *([kind] if kind else [])]
    rows = db.query('SELECT * FROM task_logs' + selected + ' ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?',
                    (*selected_args, limit, offset))
    total = db.query_one('SELECT COUNT(*) FROM task_logs' + selected, selected_args)[0]
    stats = db.query('SELECT kind,COUNT(*) AS n,COALESCE(SUM(credits),0) AS credit FROM task_logs'
                     + where + ' GROUP BY kind', params)
    by_kind = {r['kind']: {'count': r['n'], 'credits': r['credit']} for r in stats}
    return {'logs': [dict(r) for r in rows], 'total': total,
        'stats': {'by_kind': by_kind, 'total': sum(r['n'] for r in stats),
                  'total_credits': sum(r['credit'] for r in stats)},
        'realm_attribution': 'current-unambiguous-account-map' if realm else 'unfiltered',
        'deleted_or_ambiguous_realm': 'unknown'}


def checkin_page(*, mapping, realm, limit, offset, uid, days):
    local_where, local_args = db._checkin_where(uid, days)
    auto_where, auto_args = db._task_log_where(uid=uid, kind='checkin', days=days)
    scope, values = _scope(mapping, realm)
    union = ("SELECT id,ts,uid,nickname,source,kind,success,code,message,0 AS auto FROM checkin_logs"
             + local_where + " UNION ALL SELECT 1000000000+id,ts,uid,'' AS nickname,'auto' AS source,"
             "kind,CASE WHEN level='error' THEN 0 ELSE 1 END AS success,NULL AS code,message,1 AS auto "
             'FROM task_logs' + auto_where)
    args = [*local_args, *auto_args, *values]
    with_scope = ' FROM (' + union + ') WHERE ' + scope
    rows = db.query('SELECT *' + with_scope + ' ORDER BY ts DESC,auto DESC,id DESC LIMIT ? OFFSET ?',
                    (*args, limit, offset))
    total = db.query_one('SELECT COUNT(*)' + with_scope, args)[0]
    local_total = db.count_checkin_logs(uid=uid, days=days)
    auto_total = db.count_task_logs(uid=uid, kind='checkin', days=days)
    return {'items': [{**dict(r), 'success': bool(r['success']), 'auto': bool(r['auto'])} for r in rows],
        'total': total, 'local_total': local_total, 'auto_total': auto_total,
        'unfiltered_total': local_total + auto_total,
        'realm_attribution': 'current-unambiguous-account-map' if realm else 'unfiltered',
        'deleted_or_ambiguous_realm': 'unknown'}
