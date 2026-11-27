"""只读探针 2：released + 完整用量 → ReplayConflict；replay 产出的 unknown → charged_partial 不可归档。"""
import pathlib, sys, tempfile, time
sys.path.insert(0, r'D:/AI/capy/worktrees/jam_01M8EY6GJYBAZQCW18W9Y1GTKT/zhihui-gateway')
from server import budget, config, db, keysvc, ledgerreplay

tmp = tempfile.mkdtemp()
config.DB_PATH = pathlib.Path(tmp) / 'm.db'
config.USERS_FILE = pathlib.Path(tmp) / 'users.json'
db._conn = None
db.connect(); budget.initialize(force=True)
created = keysvc.create_key(name='probe2', realm='cn')
key = keysvc.resolve(created['key'])
db.execute('UPDATE api_keys SET quota=?, quota_credit=? WHERE id=?', (1_000_000, 10.0, key['id']))
now = int(time.time())

def reserve(rid, state='forwarded', age=0):
    db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (rid, key['id'], now-age, state, 4096, 0.0, now-age, 'probe', 1, now+60))

def entry(rid, terminal, complete=1, pt=17, ct=5, credit=0.123456, ts=None):
    return {'facts': {'request_id': rid, 'key_id': key['id'], 'prompt_tokens': pt, 'completion_tokens': ct,
                      'credit': credit, 'usage_complete': complete, 'usage_source': 'upstream',
                      'terminal_state': terminal, 'ts': ts or now, 'model': 'm', 'realm': 'cn'},
            'spilled_at': now}

# 1) released + 完整用量
reserve('rl-1', state='released')
try:
    with db.transaction():
        ledgerreplay._settle(entry('rl-1', 'completed'))
    print('released+complete: NO CONFLICT (unexpected)')
except ledgerreplay.ReplayConflict as exc:
    print('released+complete -> ReplayConflict:', exc)

# 2) replay 产出的 client_cancelled unknown，30 天后是否可归档
reserve('cc-1', state='forwarded', age=30*86400)
with db.transaction():
    ledgerreplay._settle(entry('cc-1', 'client_cancelled'))
row = db.query_one('SELECT state,charged_tokens,resolution FROM budget_reservations WHERE request_id=?', ('cc-1',))
print('replayed client_cancelled row:', dict(row))
cls = budget._classify(dict(row), budget._logs_for(['cc-1']).get('cc-1'))
print('  _classify(no request_log) ->', cls)
n = budget.archive_overdue_unknowns(now)
print('  archive_overdue_unknowns ->', n, '(0 = 该行永远留在 unknown)')
print('  EVIDENCELESS_CLASSES =', budget.EVIDENCELESS_CLASSES)

# 3) 归档行迟到 `用量证据` 后：summary 与 prepare 的判据是否一致
reserve('arch-late', state='unknown', age=30*86400)
db.execute('INSERT INTO request_logs(ts,key_id,status,request_id,terminal_state,usage_source,credit,prompt_tokens,completion_tokens,usage_complete) VALUES(?,?,?,?,?,?,?,?,?,?)',
           (now-30*86400, key['id'], 499, 'arch-late', 'client_cancelled', 'unknown', None, 0, 0, 0))
print('archived?', budget.archive_overdue_unknowns(now))
db.execute("UPDATE request_logs SET usage_source='upstream', credit=0.5, prompt_tokens=100, completion_tokens=10, usage_complete=1 WHERE request_id='arch-late'")
s = budget.unresolved_summary(key_id=key['id'], now=now)['summary']
print('after late evidence: needs_human=%d archived_pending=%d by_class=%s' % (s['needs_human'], s['archived_pending'], {k:v for k,v in s['by_class'].items() if v}))
held = db.query_one("SELECT COALESCE(SUM(MAX(0,reserved_tokens-charged_tokens)),0) held,"
                    " COALESCE(SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0) unknown_count"
                    " FROM budget_reservations WHERE key_id=? AND state IN ('reserved','forwarded','unknown')"
                    " AND coalesce(resolution,'') NOT LIKE ?", (key['id'], budget.ARCHIVE_RESOLUTION_PREFIX + '%'))
print('prepare 口径（排除已归档）: held=%d unknown_count=%d' % (held['held'], held['unknown_count']))
