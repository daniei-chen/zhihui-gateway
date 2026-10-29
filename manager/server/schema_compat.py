"""Audited v8 -> v7 reader compatibility for the additive instance-lease table.

This is a new v9 migration, not a rewrite of v8 implementation or user_version.
No business rows/tables or existing ownership fences are removed or restored.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from . import instancelease, schemaver

CERTIFICATE_TABLE = 'schema_reader_compatibility'
_CERT_DDL = """CREATE TABLE IF NOT EXISTS schema_reader_compatibility (
 migration_version INTEGER PRIMARY KEY,
 previous_reader INTEGER NOT NULL,
 approved_reader INTEGER NOT NULL,
 migration_checksum TEXT NOT NULL,
 layout_sha256 TEXT NOT NULL,
 certified_by TEXT NOT NULL,
 certified_at INTEGER NOT NULL
)"""


def _layout(conn):
    objects=[tuple(r) for r in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name='instance_leases' AND sql IS NOT NULL ORDER BY type,name")]
    return {'objects':objects,
            'columns':[tuple(r) for r in conn.execute('PRAGMA table_xinfo(instance_leases)')],
            'foreign_keys':[tuple(r) for r in conn.execute('PRAGMA foreign_key_list(instance_leases)')]}


def _verified_layout(conn):
    # Validate the reviewed SQL directly; opening another SQLite connection here
    # breaks the application's single-connection ownership and error wrapping.
    normalize=lambda sql:' '.join(re.sub(r'\bIF\s+NOT\s+EXISTS\b','',sql,flags=re.I).split())
    expected=[]
    for statement in instancelease.DDL:
        sql=normalize(statement)
        kind='table' if sql.startswith('CREATE TABLE ') else 'index'
        name=sql.split()[2]
        expected.append((kind,name,'instance_leases',sql))
    actual=_layout(conn)
    objects=[(kind,name,table,normalize(sql)) for kind,name,table,sql in actual['objects']]
    if objects!=sorted(expected):
        raise schemaver.SchemaDrift('v8 instance lease layout differs from reviewed additive schema')
    return hashlib.sha256(json.dumps(actual,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _v8_row(conn, expected_checksum):
    row=conn.execute('SELECT name,phase,state,required_reader,checksum FROM schema_migrations WHERE version=8').fetchone()
    if row is None or tuple(row[:3])!=('instance_leases','expand','applied') or row[4]!=expected_checksum:
        raise schemaver.SchemaDrift('v8 migration identity/state/checksum is not reviewed')
    return row


def _certificate(conn):
    return conn.execute('SELECT migration_version,previous_reader,approved_reader,migration_checksum,layout_sha256 FROM schema_reader_compatibility WHERE migration_version=8').fetchone()


def _check_certificate(row, expected_checksum, layout):
    expected=(8,8,7,expected_checksum,layout)
    if row is None or tuple(row)!=expected:
        raise schemaver.SchemaDrift('v8 reader compatibility certificate is missing or inconsistent')


def certify(conn, *, expected_checksum, release):
    """Certify only the exact reviewed expansion; retain user_version >=8 and data."""
    conn.execute('SAVEPOINT certify_v8_reader')
    try:
        row=_v8_row(conn,expected_checksum)
        if int(row[3]) not in (7,8):
            raise schemaver.SchemaDrift('v8 reader floor has an unexpected value')
        # Do not lower a floor after an unknown/contract migration or failed step.
        for version,name,phase,state,floor in conn.execute('SELECT version,name,phase,state,required_reader FROM schema_migrations'):
            if version<=8:
                if state!='applied' or phase!='expand' or (version<8 and int(floor)>7):
                    raise schemaver.SchemaDrift('legacy migration is not a reviewed applied expansion')
            elif version==9:
                if name!='instance_reader_compatibility' or phase!='expand' or state not in ('started','applied') or int(floor)!=7:
                    raise schemaver.SchemaDrift('v9 certification migration state is inconsistent')
            elif not (version==90 and phase=='contract' and state=='deferred'):
                raise schemaver.SchemaDrift('unknown or destructive migration prevents reader certification')
        layout=_verified_layout(conn)
        conn.execute(_CERT_DDL)
        existing=_certificate(conn)
        if existing is not None:
            _check_certificate(existing,expected_checksum,layout)
            if int(row[3])!=7:
                raise schemaver.SchemaDrift('certified reader floor was changed after certification')
        else:
            if int(row[3])!=8:
                raise schemaver.SchemaDrift('reader floor was lowered without a certificate')
            conn.execute('INSERT INTO schema_reader_compatibility VALUES(8,8,7,?,?,?,?)',
                         (expected_checksum,layout,str(release),int(time.time())))
            changed=conn.execute('UPDATE schema_migrations SET required_reader=7 WHERE version=8 AND required_reader=8 AND checksum=?',(expected_checksum,)).rowcount
            if changed!=1:raise schemaver.SchemaDrift('v8 reader certification lost its precondition')
        for operation in ('UPDATE','DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS schema_reader_compatibility_no_{operation.lower()} BEFORE {operation} ON schema_reader_compatibility BEGIN SELECT RAISE(ABORT,'reader certificates are immutable'); END")
        conn.execute('RELEASE SAVEPOINT certify_v8_reader')
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT certify_v8_reader')
        conn.execute('RELEASE SAVEPOINT certify_v8_reader')
        raise


def validate(conn, *, expected_checksum):
    """Startup validation: an applied certificate never silently widens compatibility."""
    row=_v8_row(conn,expected_checksum)
    if int(row[3])!=7:raise schemaver.SchemaDrift('certified v8 reader floor is inconsistent')
    _check_certificate(_certificate(conn),expected_checksum,_verified_layout(conn))
