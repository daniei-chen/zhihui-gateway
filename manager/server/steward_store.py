"""Durable alert transitions and one-recipient delivery attempts, never credentials."""
from __future__ import annotations

import json
import secrets
import time

from . import db

SCHEMA = '''
CREATE TABLE IF NOT EXISTS steward_incidents (
 id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL,
 severity TEXT NOT NULL, state TEXT NOT NULL, title TEXT NOT NULL,
 detail TEXT NOT NULL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
 resolved_at INTEGER, observations INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS steward_one_open_rule
 ON steward_incidents(rule) WHERE state='open';
CREATE INDEX IF NOT EXISTS steward_incident_time ON steward_incidents(first_seen DESC);
CREATE TABLE IF NOT EXISTS steward_deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER,
 event TEXT NOT NULL, recipient TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_at INTEGER NOT NULL, created_at INTEGER NOT NULL, finished_at INTEGER,
 owner TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
 UNIQUE(incident_id,event,recipient)
);
CREATE INDEX IF NOT EXISTS steward_delivery_due ON steward_deliveries(status,next_at);
CREATE TABLE IF NOT EXISTS steward_analysis (
 id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL,
 event TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at INTEGER NOT NULL, finished_at INTEGER, owner TEXT NOT NULL DEFAULT '',
 error_code TEXT NOT NULL DEFAULT '', UNIQUE(incident_id,event)
);
'''


def ensure_schema():
    # Use the normal serialized DB connection, not executescript's implicit commit.
    with db.transaction() as conn:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)


def observe(rule, active, severity, title, detail, *, recipients=(), recover=True, analyze=False, now=None):
    now = int(time.time() if now is None else now)
    with db.transaction() as conn:
        prior = conn.execute("SELECT * FROM steward_incidents WHERE rule=? AND state='open'", (rule,)).fetchone()
        event = None
        if active and prior is None:
            cursor = conn.execute(
                'INSERT INTO steward_incidents(rule,severity,state,title,detail,first_seen,last_seen) '
                "VALUES(?,?,'open',?,?,?,?)", (rule, severity, title, detail, now, now))
            ident, event = cursor.lastrowid, 'firing'
        elif active:
            ident = prior['id']
            conn.execute('UPDATE steward_incidents SET last_seen=?,observations=observations+1,detail=?,severity=? WHERE id=?',
                         (now, detail, severity, ident))
            if severity == 'critical' and prior['severity'] != 'critical':
                event = 'escalated'
            elif recipients:
                # Enabling notifications or adding a recipient must also report
                # an already-open incident. The unique key prevents repeated mail.
                event = 'firing'
        elif prior:
            ident, event = prior['id'], 'recovered'
            conn.execute("UPDATE steward_incidents SET state='resolved',resolved_at=?,last_seen=? WHERE id=?",
                         (now, now, ident))
            conn.execute("UPDATE steward_deliveries SET status='cancelled',finished_at=?,error_code='incident_recovered' "
                         "WHERE incident_id=? AND status='pending' AND event!='recovered'", (now, ident))
            conn.execute("UPDATE steward_analysis SET status='cancelled',finished_at=? "
                         "WHERE incident_id=? AND status='pending'", (now, ident))
        else:
            return None
        if event and (event != 'recovered' or recover):
            count = conn.execute("SELECT COUNT(*) FROM steward_deliveries WHERE status IN ('pending','sending')").fetchone()[0]
            existing = {row[0] for row in conn.execute(
                'SELECT recipient FROM steward_deliveries WHERE incident_id=? AND event=?', (ident, event))}
            new_recipients = [recipient for recipient in recipients if recipient not in existing]
            if count + len(new_recipients) > 500:
                raise RuntimeError('notification_queue_full')
            payload = json.dumps({'rule': rule, 'event': event, 'severity': severity,
                                  'title': title, 'detail': detail, 'observed_at': now,
                                  'incident_id': ident}, ensure_ascii=False)
            for recipient in new_recipients:
                conn.execute('INSERT OR IGNORE INTO steward_deliveries '
                             '(incident_id,event,recipient,payload,next_at,created_at) VALUES(?,?,?,?,?,?)',
                             (ident, event, recipient, payload, now, now))
            if analyze and new_recipients:
                queued = conn.execute("SELECT COUNT(*) FROM steward_analysis WHERE status IN ('pending','running')").fetchone()[0]
                if queued < 100:
                    conn.execute('INSERT OR IGNORE INTO steward_analysis '
                                 '(incident_id,event,payload,created_at) VALUES(?,?,?,?)',
                                 (ident, event, payload, now))
        return {'incident_id': ident, 'event': event}


def incidents(limit=50):
    return [dict(row) for row in db.query(
        'SELECT * FROM steward_incidents ORDER BY id DESC LIMIT ?', (max(1, min(100, limit)),))]


def deliveries(limit=30, *, include_recipients=False):
    # Email addresses are configuration data; read-only panel users get no address list.
    return [dict(row) for row in db.query(
        'SELECT id,incident_id,event,status,attempts,created_at,finished_at,error_code '
        + (',recipient ' if include_recipients else '') +
        'FROM steward_deliveries ORDER BY id DESC LIMIT ?', (max(1, min(100, limit)),))]


def recover_interrupted():
    """Only call after acquiring the lifetime worker lock: acceptance is unknown."""
    db.execute("UPDATE steward_deliveries SET status='unknown',error_code='worker_interrupted',finished_at=? "
               "WHERE status='sending'", (int(time.time()),))
    db.execute("UPDATE steward_analysis SET status='unknown',error_code='worker_interrupted',finished_at=? "
               "WHERE status='running'", (int(time.time()),))


def claim_analysis(owner):
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM steward_analysis WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE steward_analysis SET status='running',owner=? WHERE id=? AND status='pending'", (owner, row['id']))
        return {**dict(row), 'owner': owner}


def finish_analysis(item, answer, *, recipients=(), code=''):
    """An independent follow-up; never delay, suppress or replay the rule mail."""
    now = int(time.time())
    with db.transaction() as conn:
        incident = conn.execute('SELECT state FROM steward_incidents WHERE id=?', (item['incident_id'],)).fetchone()
        current = incident and (incident['state'] == 'resolved') == (item['event'] == 'recovered')
        payload = json.loads(item['payload'])
        delivered = current and isinstance(answer, str) and bool(answer.strip()) and bool(recipients)
        count = conn.execute("SELECT COUNT(*) FROM steward_deliveries WHERE status IN ('pending','sending')").fetchone()[0]
        if delivered and count + len(recipients) <= 500:
            payload.update(event='analysis', analysis=answer[:6000], related_event=item['event'])
            for recipient in recipients:
                conn.execute('INSERT OR IGNORE INTO steward_deliveries '
                             '(incident_id,event,recipient,payload,next_at,created_at) VALUES(?,?,?,?,?,?)',
                             (item['incident_id'], 'analysis-' + item['event'], recipient,
                              json.dumps(payload, ensure_ascii=False), now, now))
        elif delivered:
            delivered, code = False, 'notification_queue_full'
        changed = conn.execute('UPDATE steward_analysis SET status=?,finished_at=?,error_code=?,owner=? '
                               "WHERE id=? AND status='running' AND owner=?",
                               ('complete' if delivered else 'cancelled' if not current else 'failed', now,
                                code[:80], '', item['id'], item['owner'])).rowcount
        if changed != 1:
            raise RuntimeError('analysis ownership changed')


def analyses(limit=30):
    return [dict(row) for row in db.query(
        'SELECT id,incident_id,event,status,created_at,finished_at,error_code FROM steward_analysis '
        'ORDER BY id DESC LIMIT ?', (max(1, min(100, limit)),))]


def queue_state():
    result = {row['status']: row['n'] for row in db.query('SELECT status,COUNT(*) n FROM steward_deliveries GROUP BY status')}
    oldest = db.query_one("SELECT MIN(created_at) ts FROM steward_deliveries WHERE status='pending'")
    return {'counts': result, 'oldest_pending_at': oldest['ts']}


def claim(owner, *, now=None):
    now = int(time.time() if now is None else now)
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM steward_deliveries WHERE status='pending' AND next_at<=? "
                           "ORDER BY CASE WHEN event LIKE 'analysis-%' THEN 1 ELSE 0 END,id LIMIT 1",
                           (now,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE steward_deliveries SET status='sending',owner=?,attempts=attempts+1 WHERE id=? AND status='pending'",
                     (owner, row['id']))
        return {**dict(row), 'owner': owner, 'attempts': row['attempts'] + 1}


def finish(item, outcome, *, now=None):
    now = int(time.time() if now is None else now)
    status = outcome['status']
    if status not in ('accepted', 'retry', 'rejected', 'unknown', 'cancelled'):
        raise ValueError('invalid delivery outcome')
    retry = status == 'retry' and item['attempts'] < 3
    stored = 'pending' if retry else 'failed' if status == 'retry' else status
    delay = (60, 300, 1800)[min(item['attempts'] - 1, 2)]
    with db.transaction() as conn:
        changed = conn.execute(
            'UPDATE steward_deliveries SET status=?,error_code=?,next_at=?,finished_at=?,owner=? '
            "WHERE id=? AND status='sending' AND owner=?",
            (stored, outcome.get('code', '')[:80], now + delay, None if retry else now,
             '', item['id'], item['owner'])).rowcount
        if changed != 1:
            raise RuntimeError('delivery ownership changed')


def queue_test(recipient, *, actor):
    now = int(time.time())
    # No arbitrary body, sender, URL or subject can be used as an email relay.
    with db.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) FROM steward_deliveries WHERE status IN ('pending','sending')").fetchone()[0]
        if count >= 500:
            raise ValueError('notification_queue_full')
        return conn.execute('INSERT INTO steward_deliveries '
                            '(event,recipient,payload,next_at,created_at) VALUES(?,?,?,?,?)',
                            ('test-' + secrets.token_hex(8), recipient, json.dumps({
                                'event': 'test', 'severity': 'info', 'title': '智汇管家 · 通知链路测试',
                                'detail': '这是一封由超级管理员主动发起的测试邮件，不代表网关发生故障。',
                                'observed_at': now, 'initiated_by': actor}, ensure_ascii=False), now, now)).lastrowid


def prune(now=None):
    """Keep open incidents and every uncertain delivery; bound successful history."""
    cutoff = int(time.time() if now is None else now) - 90 * 86400
    with db.transaction() as conn:
        conn.execute("DELETE FROM steward_analysis WHERE status IN ('complete','cancelled','failed') "
                     'AND created_at<? AND id NOT IN (SELECT id FROM steward_analysis ORDER BY id DESC LIMIT 1000)', (cutoff,))
        conn.execute("DELETE FROM steward_deliveries WHERE status IN ('accepted','cancelled','rejected','failed') "
                     'AND created_at<? AND id NOT IN (SELECT id FROM steward_deliveries ORDER BY id DESC LIMIT 2000)', (cutoff,))
        conn.execute("DELETE FROM steward_incidents WHERE state='resolved' AND resolved_at<? "
                     'AND id NOT IN (SELECT incident_id FROM steward_deliveries WHERE incident_id IS NOT NULL) '
                     'AND id NOT IN (SELECT incident_id FROM steward_analysis) '
                     'AND id NOT IN (SELECT id FROM steward_incidents ORDER BY id DESC LIMIT 1000)', (cutoff,))
