"""Synthetic read failures and multi-realm data may not become invented zeros."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from .test_merge_remaining_boundaries import local_db


def adapter(path):
    from server.qtgcore.adapters.workbuddy import WorkBuddyAdapter
    obj=WorkBuddyAdapter.__new__(WorkBuddyAdapter)
    obj.cfg=SimpleNamespace(wb_read_mode='sqlite',wb_db=str(path))
    return obj


def test_missing_and_unrecognized_database_fail_instead_of_empty_success(tmp_path):
    from server.qtgcore import manager_api
    obj=adapter(tmp_path/'missing')
    with pytest.raises(manager_api.UpstreamPayloadError):obj._db()
    (tmp_path/'missing').write_bytes(b'not a database')
    with pytest.raises(manager_api.UpstreamPayloadError):obj._db()


def test_required_query_failure_is_reported(local_db):
    from server import config
    from server.qtgcore import manager_api
    obj=adapter(config.DB_PATH)
    local_db.execute('DROP TABLE usage_hourly')
    with pytest.raises(manager_api.UpstreamPayloadError):obj._usage_hourly_from_sql('2026-11-05','cn')


def test_realm_usage_and_errors_stay_separate_and_legacy_counts_unknown():
    obj=adapter('unused')
    rows=[('2026-11-05','model',5,100,20,1,'cn'),
          ('2026-11-05','model',7,200,30,2,'global')]
    result=obj._build_usage(1,rows,{('2026-11-05','cn'):1,('2026-11-05','global'):3})
    buckets={r['realm']:r for r in result['usage']}
    assert buckets['cn']['requests']==5 and buckets['cn']['errors']==1
    assert buckets['global']['requests']==7 and buckets['global']['errors']==3
    from server.qtgcore import manager_api
    with pytest.raises(manager_api.UpstreamPayloadError,match='地区归属'):
        obj._build_usage(1,rows,{'2026-11-05':4})
    unknown=obj._build_usage(1,[('2026-11-05','model',1,2,3,0,None)],
                             {('2026-11-05','unknown'):1})
    assert len(unknown['usage'])==1
    assert unknown['usage'][0]['realm']=='unknown' and unknown['usage'][0]['errors']==1


def test_filtered_income_is_unknown_without_historical_realm_evidence(local_db):
    from server import config
    result=adapter(config.DB_PATH)._credits_flow_from_sql(1,'cn')
    assert result['total']['income'] is None and result['total']['net'] is None


def test_audit_cross_process_append_is_serialized_and_partial_tail_refused(tmp_path):
    from server.qtgcore.audit import AuditLog
    path=tmp_path/'audit.jsonl'
    log=AuditLog(path)
    code="from pathlib import Path; from server.qtgcore.audit import AuditLog; import sys; a=AuditLog(Path(sys.argv[1])); [a.append('synthetic',target=str(i)) for i in range(8)]"
    tasks=[subprocess.Popen([sys.executable,'-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
           for _ in range(3)]
    try:
        for task in tasks:
            out,err=task.communicate(timeout=40)
            assert task.returncode==0,err.decode(errors='replace')
    finally:
        for task in tasks:
            if task.poll() is None:task.kill();task.communicate(timeout=10)
    assert log.verify()['ok'] and log.verify()['records']==24
    assert len(log.tail(5))==5
    path.write_bytes(path.read_bytes().rstrip(b'\n'))
    before=path.read_bytes()
    with pytest.raises(ValueError,match='unterminated'):log.append('must-refuse')
    assert path.read_bytes()==before


def test_credential_acquisition_cannot_take_a_free_lock_after_deadline(tmp_path,monkeypatch):
    from server.services import credentiallease
    clock=[0.0]
    monkeypatch.setattr(credentiallease,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    attempts=[]
    def lock(fd):
        attempts.append(fd)
        return len(attempts)>1
    monkeypatch.setattr(credentiallease,'_try_lock',lock)
    monkeypatch.setattr(credentiallease,'_unlock',lambda fd:None)
    real_sleep=asyncio.sleep
    async def sleep(delay):
        clock[0]=1.0
        await real_sleep(0)
    monkeypatch.setattr(credentiallease.asyncio,'sleep',sleep)
    async def run():
        with pytest.raises(TimeoutError):
            async with credentiallease.acquire(tmp_path/'credential',timeout=0.01):
                pytest.fail('expired acquire entered its protected body')
    asyncio.run(run())
    assert len(attempts)==1
