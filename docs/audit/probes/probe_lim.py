"""只读探针 4：budget.unresolved() 的 1000 行上限对 /resolve-batch 的影响。"""
import pathlib, sys, tempfile, time
sys.path.insert(0, r'D:/AI/capy/worktrees/jam_01M8EY6GJYBAZQCW18W9Y1GTKT/zhihui-gateway')
from server import budget, config, db, keysvc
tmp = tempfile.mkdtemp()
config.DB_PATH = pathlib.Path(tmp) / 'm.db'
config.USERS_FILE = pathlib.Path(tmp) / 'users.json'
db._conn = None; db.connect(); budget.initialize(force=True)
key = keysvc.resolve(keysvc.create_key(name='probe4', realm='cn')['key'])
now = int(time.time())
rows = [(f'r{i:04d}', key['id'], now-100000+i, 'unknown', 1000, 0.0, now-100000+i, '', 1, 0) for i in range(1005)]
db._conn.executemany("INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
db._conn.commit()
u = budget.unresolved()
ids = {r['request_id'] for r in u}
print('表内 unknown 总数:', db.query_one("SELECT count(*) n FROM budget_reservations WHERE state='unknown'")['n'])
print('unresolved() 返回:', len(u), ' 最新一行 r1004 在内?', 'r1004' in ids, ' 最老 r0000 在内?', 'r0000' in ids)
# resolve-batch 的 pending 映射就建在这个返回值上（routers/budgets.py:93）
print('对 r1004 调 resolve-batch 会得到 -> not_pending')
