"""只读探针 5：keysvc.reset_usage 是否清 key_daily_usage（日配额）。"""
import pathlib, sys, tempfile, time
sys.path.insert(0, r'D:/AI/capy/worktrees/jam_01M8EY6GJYBAZQCW18W9Y1GTKT/zhihui-gateway')
from server import budget, config, db, keysvc, dailyquota, observability

tmp = tempfile.mkdtemp()
config.DB_PATH = pathlib.Path(tmp) / 'm.db'
config.USERS_FILE = pathlib.Path(tmp) / 'users.json'
db._conn = None; db.connect(); budget.initialize(force=True)
created = keysvc.create_key(name='reset-probe', realm='cn')
key = keysvc.resolve(created['key'])
db.execute('UPDATE api_keys SET daily_token_quota=? WHERE id=?', (1000, key['id']))
keysvc.touch(key, '127.0.0.1', 1000, 0)
dailyquota.charge(key['id'], 1000, 0, int(time.time()))
print('before reset:', dict(db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (key['id'],))),
      dict(db.query_one('SELECT tokens FROM key_daily_usage WHERE key_id=?', (key['id'],))))
print('reset_usage ->', keysvc.reset_usage(key['id'], actor='audit-probe'))
print('after reset:', dict(db.query_one('SELECT used_tokens FROM api_keys WHERE id=?', (key['id'],))),
      dict(db.query_one('SELECT tokens FROM key_daily_usage WHERE key_id=?', (key['id'],))))
# 现在对同一密钥走真实 prepare，看是否仍被日配额拒绝
class S:
    request_id='probe-reset-1'; key_id=key['id']; budget_fence=1; budget_reserved=False
    terminal_state=None; http_status=0; error_stage=None
observability.current.set(S)
try:
    budget.prepare(key, {'model':'deepseek-v4-flash','messages':[{'role':'user','content':'hi'}]})
    print('prepare after reset -> 通过')
except budget.BudgetRejected as exc:
    print('prepare after reset -> BudgetRejected', exc.code)
