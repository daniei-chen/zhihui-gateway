"""Conservative local-lab snapshot restore guard; never a production rollback tool.

All writers MUST already be stopped by the caller. This comparison is a refusal
barrier, not writer election or a replacement for a lossless schema downgrade.
Only explicit lease/migration bookkeeping may differ. Unknown/schema changes
require manual recovery. Never log account values, tokens or request bodies.
"""
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

BOOKKEEPING = frozenset({'instance_leases', 'schema_migrations', 'schema_backfill_progress'})


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _value(value):
    if isinstance(value, bytes):
        return ['blob', value.hex()]
    return [type(value).__name__, value]


def _catalog(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _fingerprint(conn, table):
    quoted = _quote(table)
    columns = [tuple(row) for row in conn.execute('PRAGMA table_info('+quoted+')')]
    rows = []
    for row in conn.execute('SELECT * FROM '+quoted):
        encoded = json.dumps([_value(v) for v in row], separators=(',', ':'), ensure_ascii=True).encode()
        rows.append(hashlib.sha256(encoded).digest())
    digest = hashlib.sha256()
    for row_hash in sorted(rows):
        digest.update(row_hash)
    return {'columns': columns, 'rows': len(rows), 'sha256': digest.hexdigest()}


def compare_business_data(current, snapshot):
    """Fail closed on missing tables, changed values, schema or new populated tables."""
    record = {'safe_to_restore': False, 'changed_tables': [], 'ignored_tables': sorted(BOOKKEEPING)}
    try:
        with closing(sqlite3.connect(Path(current).resolve().as_uri()+'?mode=ro', uri=True)) as live, closing(sqlite3.connect(Path(snapshot).resolve().as_uri()+'?mode=ro', uri=True)) as old:
            live.execute('BEGIN')
            old.execute('BEGIN')
            old_tables, live_tables = _catalog(old), _catalog(live)
            for name in sorted((old_tables | live_tables) - BOOKKEEPING):
                if name not in live_tables:
                    record['changed_tables'].append(name)
                elif name not in old_tables:
                    if live.execute('SELECT 1 FROM '+_quote(name)+' LIMIT 1').fetchone() is not None:
                        record['changed_tables'].append(name)
                elif _fingerprint(live, name) != _fingerprint(old, name):
                    record['changed_tables'].append(name)
        record['safe_to_restore'] = not record['changed_tables']
    except Exception as exc:
        record['error'] = type(exc).__name__
    return record


def probe_old_reader(db_path, source):
    """Read-only preflight for the reviewed lab v7 <-> v9 rollback pair."""
    import ast
    import sys
    result={'readable':False,'mode':'same_database','reason':None}
    try:
        migrations_file=Path(source)/'manager/server/migrations.py'
        text=migrations_file.read_text(encoding='utf8')
        tree=ast.parse(text)
        versions=[];reader=None
        for node in ast.walk(tree):
            if isinstance(node,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='CODE_SCHEMA_VERSION' for x in node.targets):
                reader=ast.literal_eval(node.value)
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='Step':
                versions.extend(ast.literal_eval(k.value) for k in node.keywords if k.arg=='version')
        if not isinstance(reader,int) or isinstance(reader,bool) or not versions:
            raise ValueError('old reader version cannot be determined')
        result.update({'reader_version':reader,'reader_migrations_sha256':hashlib.sha256(text.encode()).hexdigest()})
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            conn.execute('BEGIN')
            rows=conn.execute("SELECT version,required_reader FROM schema_migrations WHERE state='applied'").fetchall()
            if not rows:raise ValueError('versioned database required')
            need=max(int(r[1]) for r in rows)
            stored=conn.execute('PRAGMA user_version').fetchone()[0]
            result.update({'required_reader':need,'user_version':stored})
            if need>reader or stored>max(versions):
                result['reason']='reader_too_old';return result
            if max(r[0] for r in rows)>reader:
                # Broader unknown/future compatibility is deliberately not inferred.
                if not (reader>=7 and stored==9 and max(r[0] for r in rows)==9):
                    result['reason']='uncertified_newer_schema';return result
                manager=Path(__file__).resolve().parents[2]/'manager'
                if str(manager) not in sys.path:sys.path.insert(0,str(manager))
                from server import migrations as candidate_migrations, schema_compat
                v8=next(s for s in candidate_migrations.MANAGER_STEPS if s.version==8)
                schema_compat.validate(conn,expected_checksum=v8.checksum)
                record=conn.execute("SELECT name,phase,state,required_reader FROM schema_migrations WHERE version=9").fetchone()
                if tuple(record or ())!=('instance_reader_compatibility','expand','applied',7):
                    raise ValueError('compatibility migration is not certified')
                result['certificate_verified']=True
            result['readable']=True
    except Exception as exc:
        result['reason']=type(exc).__name__+': '+str(exc)[:160]
    return result
