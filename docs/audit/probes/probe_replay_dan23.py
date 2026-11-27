"""只读探针：ledgerreplay._settle 是否镜像 DAN-23 的 settled 契约。"""
import pathlib, sys, tempfile, time
sys.path.insert(0, r'D:/AI/capy/worktrees/jam_01M8EY6GJYBAZQCW18W9Y1GTKT/zhihui-gateway')
from server import budget, config, db, keysvc, ledgerreplay

tmp = tempfile.mkdtemp()
config.DB_PATH = pathlib.Path(tmp) / 'm.db'
config.USERS_FILE = pathlib.Path(tmp) / 'users.json'
db._conn = None
db.connect()
budget.initialize(force=True)
created = keysvc.create_key(name='probe', realm='cn')
key = keysvc.resolve(created['key'])
db.execute('UPDATE api_keys SET quota=?, quota_credit=? WHERE id=?', (1_000_000, 10.0, key['id']))
now = int(time.time())

def make(rid, terminal, complete=1, pt=17, ct=5, credit=0.123456, state='forwarded'):
    db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (rid, key['id'], now, state, 4096, 0.0, now, 'probe-owner', 1, now+60))
    return {'facts': {'request_id': rid, 'key_id': key['id'], 'prompt_tokens': pt, 'completion_tokens': ct,
                      'credit': credit, 'usage_complete': complete, 'usage_source': 'upstream',
                      'terminal_state': terminal, 'ts': now, 'model': 'm', 'realm': 'cn'},
            'spilled_at': now}

for terminal in ('completed', 'client_cancelled'):
    rid = 'probe-' + terminal
    entry = make(rid, terminal)
    with db.transaction():
        plan = ledgerreplay._settle(entry)
    row = db.query_one('SELECT state,charged_tokens,charged_credit FROM budget_reservations WHERE request_id=?', (rid,))
    print(f'{terminal:17s} -> state={row["state"]:9s} charged_tokens={row["charged_tokens"]} charged_credit={row["charged_credit"]} plan={plan["tokens"]}')
