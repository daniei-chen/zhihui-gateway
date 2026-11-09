"""Independent daily enforcement counters; report rebuild/reset must not erase them."""
from __future__ import annotations

from . import db, ledger


def seed_existing(conn) -> None:
    """Freeze the pre-feature aggregate before report maintenance can rebuild it."""
    if conn.execute("SELECT 1 FROM settings WHERE key='quota.daily-seed.v1'").fetchone():
        return
    conn.execute('INSERT OR IGNORE INTO key_daily_usage(key_id,day,tokens,credit_units) '
                 'SELECT key_id,day,sum(prompt_tokens+completion_tokens),sum(credit_units) '
                 'FROM usage_daily GROUP BY key_id,day')
    conn.execute('INSERT OR IGNORE INTO key_daily_usage(key_id,day,tokens,credit_units) '
                 'SELECT id,?,0,0 FROM api_keys', (db.day_of(),))
    conn.execute("INSERT INTO settings(key,value) VALUES('quota.daily-seed.v1','1')")


def snapshots(day: str) -> dict:
    """Batch list projection, without one aggregate query for every key."""
    rows = db.query('SELECT key_id,sum(prompt_tokens+completion_tokens) tokens,'
                     'sum(credit_units) credit_units FROM usage_daily WHERE day=? GROUP BY key_id', (day,))
    result = {r['key_id']: {'tokens': r['tokens'], 'credit_units': r['credit_units']} for r in rows}
    result.update({r['key_id']: {'tokens': r['tokens'], 'credit_units': r['credit_units']}
                   for r in db.query('SELECT key_id,tokens,credit_units FROM key_daily_usage WHERE day=?', (day,))})
    return result


def snapshot(key_id: int, day: str) -> dict:
    row = db.query_one('SELECT tokens,credit_units FROM key_daily_usage WHERE key_id=? AND day=?',
                       (key_id, day))
    if row is not None:
        return dict(row)
    # Seed pre-feature traffic from the existing aggregate once, under the caller's
    # transaction. Reads do not seed: merely viewing a key never changes its budget.
    old = db.query_one('SELECT coalesce(sum(prompt_tokens+completion_tokens),0) tokens,'
                       'coalesce(sum(credit_units),0) credit_units FROM usage_daily WHERE key_id=? AND day=?',
                       (key_id, day))
    return dict(old)


def charge(key_id: int, tokens: int, credit: float | None, ts=None) -> None:
    day = db.day_of(ts)
    with db.transaction():
        initial = snapshot(key_id, day)
        db.execute('INSERT OR IGNORE INTO key_daily_usage(key_id,day,tokens,credit_units) VALUES(?,?,?,?)',
                   (key_id, day, initial['tokens'], initial['credit_units']))
        db.execute('UPDATE key_daily_usage SET tokens=max(0,tokens+?),credit_units=max(0,credit_units+?) '
                   'WHERE key_id=? AND day=?', (tokens, ledger.units(credit or 0), key_id, day))
