"""只读探针 3：面板 resolve 通道对 legacy unknown 行是否可用。"""
import pathlib, sys, tempfile, time
sys.path.insert(0, r'D:/AI/capy/worktrees/jam_01M8EY6GJYBAZQCW18W9Y1GTKT/zhihui-gateway')
from server import budget, config, db, keysvc, budgetowner

tmp = tempfile.mkdtemp()
config.DB_PATH = pathlib.Path(tmp) / 'm.db'
config.USERS_FILE = pathlib.Path(tmp) / 'users.json'
db._conn = None
db.connect(); budget.initialize(force=True)
key = keysvc.resolve(keysvc.create_key(name='probe3', realm='cn')['key'])
now = int(time.time())

def reserve(rid, owner):
    db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (rid, key['id'], now-30*86400, 'unknown', 5000, 0.0, now-30*86400, owner, 1, 0))

# A: legacy 行（owner=''），无 request_lifetime_ends_v2
reserve('legacy-1', '')
try:
    budget.resolve('legacy-1', 0, 0.0, 'admin', '人工核对：历史超时无证据', human_evidence='2026-11-28 审计复核，确认无上游用量')
    print('legacy + evidence -> resolved (unexpected)')
except ValueError as exc:
    print('legacy + evidence -> ValueError:', exc)

# B: 同一行，不带 evidence
print('legacy 无 evidence ->', budget.resolve('legacy-1', 0, 0.0, 'admin', '人工核对：历史超时无证据'))

# C: 普通行（owner 是本机 identity，进程存活），无 request_lifetime_ends_v2
reserve('live-1', budgetowner.identity())
try:
    budget.resolve('live-1', 0, 0.0, 'admin', '人工核对：历史超时无证据', human_evidence='2026-11-28 审计复核，确认无上游用量')
    print('live-owner + evidence -> resolved (unexpected)')
except ValueError as exc:
    print('live-owner + evidence -> ValueError:', exc)

# D: 有 request_lifetime_ends_v2 的正常行
reserve('ended-1', budgetowner.identity())
db.execute('INSERT OR IGNORE INTO request_lifetime_ends_v2 VALUES(?,?,?,?)', ('ended-1', key['id'], budgetowner.identity(), now))
print('ended + evidence ->', budget.resolve('ended-1', 0, 0.0, 'admin', '人工核对：历史超时无证据', human_evidence='2026-11-28 审计复核，确认无上游用量'))
