"""A(v7) -> B(v9) writes -> A2(v7), same DB, real TLS, streams and budgets.

Disposable local VM only. The v7 target is a pinned QA snapshot, not a claim
that an uncollected production release is automatically rollback-compatible.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import time
from unittest.mock import patch
import r4_parallel_shadow as r4
from r3l2_handoff import HandoffExperiment, LAUNCHER


def verify_source(repo, manifest):
    data=json.loads(manifest.read_text())
    for entry in data['source_manifest']:
        target=repo/entry['path']
        if not target.resolve().is_relative_to(repo.resolve()) or not target.is_file():
            raise RuntimeError('Pinned A file missing or outside snapshot: '+entry['path'])
        if hashlib.sha256(target.read_bytes()).hexdigest()!=entry['sha256']:
            raise RuntimeError('Pinned A snapshot was modified: '+entry['path'])
    return {'sha256':data['archive_sha256'],'files_verified':len(data['source_manifest'])}


def business_snapshot(path):
    tables=('api_keys','request_logs','usage_events','usage_daily','usage_hourly',
            'budget_reservations','budget_adjustments','forwarding_events',
            'adjustment_events','attempt_events','schema_reader_compatibility')
    record={}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('BEGIN')
        for table in tables:
            rows=[]
            for row in conn.execute('SELECT * FROM '+table):
                encoded=json.dumps([['blob',x.hex()] if isinstance(x,bytes) else [type(x).__name__,x] for x in row],separators=(',',':')).encode()
                rows.append(hashlib.sha256(encoded).hexdigest())
            record[table]={'rows':len(rows),'sha256':hashlib.sha256(''.join(sorted(rows)).encode()).hexdigest()}
        record['schema']={'user_version':conn.execute('PRAGMA user_version').fetchone()[0],
                          'required_reader':conn.execute("SELECT max(required_reader) FROM schema_migrations WHERE state='applied'").fetchone()[0]}
    return record


def create_limited_cancel_key(instance, *, quota=1_000_000):
    """Create a token-limited synthetic key through the real Manager admin API.

    An unlimited key never reserves tokens: ``budget.prepare`` only computes the
    conservative reservation when ``key.quota`` (or ``quota_credit``) is set, so
    the cancelled-request budget row would carry ``reserved_tokens=0``.  The
    cancellation scenario therefore needs its own limited key.  Only this key is
    limited; the three-platform / three-protocol coverage keeps the original
    unlimited keys.
    """
    response = instance.session.post('/api/keys', json={
        'name': 'lossless-cancel-limited',
        'platforms': ['workbuddy'],
        'quota': quota,
    })
    if response.status_code != 200:
        raise AssertionError('Manager key API rejected the limited synthetic key: '
                             + str(response.status_code) + ' ' + response.text[:300])
    created = response.json()
    token = created.get('key') or ''
    if not token or int(created.get('quota') or 0) != quota:
        raise AssertionError('Manager key API did not return a token-limited key')
    return {'key': token, 'id': int(created['id']), 'quota': int(created['quota']),
            'prefix': created.get('prefix'), 'platforms': created.get('platforms')}


def drain_to_zero(exp, instance, timeout=60):
    record=exp.drain_action(instance,'begin')
    if record['status']!=200:raise AssertionError('drain rejected')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        response=instance.session.get('/api/system/drain')
        data=response.json()
        if response.status_code==200 and data.get('total_in_flight')==0:return record
        time.sleep(.1)
    raise AssertionError('In-flight did not reach zero before stop')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--qa-file',required=True);parser.add_argument('--a-repo',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args();repo=Path(__file__).resolve().parents[2];old=Path(args.a_repo).resolve();out=Path(args.out).resolve();lab=Path('/home/gateway-lab/lab')
    if socket.gethostname()!='zhihui-local-lab' or Path.home()!=lab.parent or not repo.is_relative_to(lab/'snapshots') or not old.is_relative_to(lab/'snapshots') or old==repo or not out.is_relative_to(lab) or out.exists():raise RuntimeError('Disposable local VM snapshots and unique evidence only')
    qa=json.loads(Path(args.qa_file).read_text())
    if qa['production_connected'] is not False or Path(qa['workspace']).resolve()!=repo:raise RuntimeError('QA source mismatch')
    pinned=verify_source(old,repo/'tests/linux_lab/fixtures/rollback-v7-source-manifest.json')
    exp=HandoffExperiment(argparse.Namespace(repo=str(repo),out=str(out),chunk_delay=.05,chunk_count=12,instance_ttl=30))
    report={'status':'running','production_connected':False,'sources':{'a':pinned,'b':{'sha256':qa['source_sha256']}},'phases':{},'limits':['Synthetic providers/accounts only','Pinned QA v7 target; production target must be separately pinned and signed','No production downtime estimate']}
    cleanup=[];entry=None;holder=None
    try:
        # Add an explicit non-zero synthetic charge without changing product accounting.
        import sys
        sys.path.insert(0,str(repo/'tests'))
        from fake_provider.server import FakeProvider
        original_factory=FakeProvider._make_handler
        def charged_factory(provider):
            Parent=original_factory(provider)
            class Charged(Parent):
                def with_charge(self,obj):
                    if isinstance(obj,dict) and isinstance(obj.get('usage'),dict):
                        return {**obj,'usage':{**obj['usage'],'credit':.125}}
                    return obj
                def _sse(self,obj,*a,**kw):
                    return super()._sse(self.with_charge(obj),*a,**kw)
                def _send_json(self,code,payload,*a,**kw):
                    return super()._send_json(code,self.with_charge(payload),*a,**kw)
            return Charged
        with patch.object(FakeProvider,'_make_handler',charged_factory):exp.prepare()
        report['synthetic_usage_credit']=.125
        exp.env['ZH_INSTANCE_HEARTBEAT_SEC']='10' 
        old_view=r4.InstanceView(exp,source=old,env={**exp.env,'WB_APP_VERSION':'lab-v7'},root=exp.root/'old-a',label='old-a')
        b_view=r4.InstanceView(exp,source=repo,env={**exp.env,'WB_APP_VERSION':'lab-v9'},root=exp.root/'new-b',label='new-b')
        low_kdf=lambda text:text.replace("security.make_hash('r3l2-synthetic-password')","security.make_hash('r3l2-synthetic-password', iterations=1000)")
        a_info=r4.start_instance(old_view,'A-v7',ready_timeout=90,launcher_text=low_kdf(r4.OLD_LAUNCHER),cleanup=cleanup)
        a=a_info['instance']
        if not a_info['ready']['ready']:raise AssertionError('A not ready')
        entry=r4.Entry(exp,'lossless',a.port);entry.start();original_conf=entry.conf.read_bytes()
        cases=[dict(c) for c in r4.CASES[:3]]+[dict(r4.CASES[0],name='trae-chat-stream',platform='trae',path='/trae/v1/chat/completions')]
        def requests(tag, keys):
            records=[]
            for case in cases:
                result=r4.run_case(entry.url,keys[case['platform']],case,timeout=60,rel_ms=exp.timeline.rel_ms)
                if result['status']!=200 or result['terminal']!='completed' or not result.get('execution_id'):
                    raise AssertionError(tag+': '+case['name']+' failed: '+str({k:result.get(k) for k in ('status','terminal','error')}))
                records.append(result)
            report['phases'][tag]=records
            return records
        old_requests=requests('a_before_upgrade',a_info['info']['keys'])
        drain_to_zero(exp,a);r4.stop_instance(a,reason='serial_upgrade')
        if a.proc.poll() is None or not r4.port_closed(a.port):raise AssertionError('A writer not stopped')
        snapshot=exp.root/'pre-switch.db';backup=r4.backup_main_db(exp.db_path,snapshot)
        if not backup['ok']:raise AssertionError('Pre-switch backup failed')
        b_info=r4.start_instance(b_view,'B-v9',ready_timeout=90,launcher_text=low_kdf(LAUNCHER),cleanup=cleanup)
        b=b_info['instance']
        if not b_info['ready']['ready']:raise AssertionError('B not ready')
        r4.switch_upstream.set_upstream_ports(entry.state,[b.port]);r4.switch_upstream.reload_nginx(entry.state)
        if not entry.wait_front(30)['front_200']:raise AssertionError('B entry not ready')
        b_requests=requests('b_after_upgrade',b_info['info']['keys'])
        # H01: an unlimited key reserves zero tokens, so the cancellation sample
        # must run on a token-limited key created through the real admin API.
        limited=create_limited_cancel_key(b)
        report['limited_cancel_key']={'id':limited['id'],'quota':limited['quota'],
                                      'prefix':limited['prefix'],'platforms':limited['platforms'],
                                      'token_sha256':hashlib.sha256(limited['key'].encode()).hexdigest()}
        cancel=r4.run_case(entry.url,limited['key'],r4.CASE_BY_NAME['client-cancel'],timeout=60,rel_ms=exp.timeline.rel_ms)
        if cancel['status']!=200 or not cancel['cancelled']:raise AssertionError('B cancellation did not occur')
        # A terminal [DONE] would mean usage had already come back; the unknown
        # reservation may only be demanded on a sample that never saw it.
        if cancel['saw_done']:raise AssertionError('Cancellation sample received terminal [DONE]; unknown-usage claim invalid')
        report['cancel']=cancel
        # A live B stream is admitted before drain, then must finish through it.
        exp.provider.chunk_count=80
        holder=r4.spawn_entry_stream(entry.url,b_info['info']['keys']['workbuddy'],r4.INFLIGHT_CASE,timeout=60,rel_ms=exp.timeline.rel_ms)
        if not r4.wait_frames(holder,30) or not holder['thread'].is_alive():raise AssertionError('No live B stream before drain')
        begin_drain=exp.drain_action(b,'begin')
        if begin_drain['status']!=200:raise AssertionError('B drain control failed')
        stop_admission=begin_drain['t_end_ms']
        report['admission_stop_ms']=stop_admission
        rejected=r4.run_case(entry.url,b_info['info']['keys']['workbuddy'],r4.CASE_BY_NAME['chat-nonstream'],timeout=10,rel_ms=exp.timeline.rel_ms)
        if rejected['status']!=503:raise AssertionError('Drain admitted a new request')
        holder['thread'].join(60)
        if holder['thread'].is_alive() or holder['record']['terminal']!='completed':raise AssertionError('Admitted B stream was cut')
        drain_to_zero(exp,b);r4.stop_instance(b,reason='forced_release_rollback')
        if b.proc.poll() is None:raise AssertionError('B writer remains alive')
        db_identity_before=(Path(exp.db_path).stat().st_dev,Path(exp.db_path).stat().st_ino)
        before=business_snapshot(exp.db_path)
        report['business_before_rollback']=before
        with closing(sqlite3.connect(Path(exp.db_path).resolve().as_uri()+'?mode=ro',uri=True)) as connection:
            monetary=connection.execute('SELECT coalesce(sum(credit_units),0) FROM usage_events WHERE counted=1').fetchone()[0]
            held=connection.execute('SELECT state,reserved_tokens,charged_tokens,reserved_credit,charged_credit,key_id FROM budget_reservations WHERE request_id=?',(cancel['execution_id'],)).fetchone()
            key_row=connection.execute('SELECT quota,used_tokens,platforms FROM api_keys WHERE id=?',(limited['id'],)).fetchone()
        if monetary<=0:raise AssertionError('No positive billed credit was exercised')
        if key_row is None or not key_row[0] or int(key_row[0])!=limited['quota']:
            raise AssertionError('Token-limited cancel key quota was not persisted')
        if held is None or held[0]!='unknown' or held[1]<=0:raise AssertionError('Cancelled unknown-usage budget was not retained')
        if held[5]!=limited['id']:raise AssertionError('Cancelled budget row does not belong to the token-limited key')
        report['positive_billed_credit_units_before']=monetary
        report['limited_cancel_key']['db_quota']=int(key_row[0])
        report['limited_cancel_key']['db_platforms']=key_row[2]
        report['cancelled_budget_before']={'state':held[0],'reserved_tokens':held[1],'charged_tokens':held[2],
                                           'reserved_credit':held[3],'charged_credit':held[4],'key_id':held[5]}

        report['snapshot_would_lose_new_business']=not r4.compare_business_data(exp.db_path,snapshot)['safe_to_restore']
        if not report['snapshot_would_lose_new_business'] or before['schema']!={'user_version':9,'required_reader':7}:raise AssertionError('No certified v9 business state before rollback')
        with patch.object(r4,'restore_main_db',wraps=r4.restore_main_db) as restore:
            recovery=r4.restore_a_service(exp,old_view,entry,old_port=a.port,original_conf=original_conf,timeout=90,rel_ms=exp.timeline.rel_ms,cleanup=cleanup,db_snapshot=str(snapshot),db_snapshot_sha256=backup['sha256'],db_path=exp.db_path,live_instance=a,writer_processes=list(cleanup))
            report['snapshot_restore_calls']=restore.call_count
        report['recovery']=recovery
        if not recovery['pass'] or restore.call_count or recovery['db_restored']:raise AssertionError('Lossless recovery failed')
        a2=cleanup[-1]
        after=business_snapshot(exp.db_path);report['business_after_rollback']=after
        with closing(sqlite3.connect(Path(exp.db_path).resolve().as_uri()+'?mode=ro',uri=True)) as connection:
            held_after=connection.execute('SELECT state,reserved_tokens,charged_tokens FROM budget_reservations WHERE request_id=?',(cancel['execution_id'],)).fetchone()
        if held_after is None:raise AssertionError('Unknown-usage budget row disappeared after old-reader restart')
        report['cancelled_budget_after']={'state':held_after[0],'reserved_tokens':held_after[1],'charged_tokens':held_after[2]}
        db_identity_after=(Path(exp.db_path).stat().st_dev,Path(exp.db_path).stat().st_ino)
        report['database_identity']={'before':db_identity_before,'after':db_identity_after}
        if before!=after:raise AssertionError('Existing B business rows changed during old reader restart')
        first=r4.run_case(entry.url,a_info['info']['keys']['workbuddy'],r4.CASE_BY_NAME['chat-nonstream'],timeout=60,rel_ms=exp.timeline.rel_ms)
        if first['status']!=200:raise AssertionError('No admission after rollback')
        report['T_admission_ms']=round(first['t_end_ms']-stop_admission,3)
        report['post_rollback_first']=first
        exp.provider.chunk_count=12
        final_requests=requests('a2_after_rollback',a_info['info']['keys'])
        ids=[r['execution_id'] for r in old_requests+b_requests+final_requests]+[holder['record']['execution_id'],first['execution_id']]
        complete=r4.ledger_completeness(exp.db_path,ids);ledger=r4.ledger_snapshot(exp.db_path)
        report['ledger_completeness']=complete;report['ledger']=ledger
        report['checks']={'positive_billed_credit_preserved':monetary>0 and before==after,'cancel_key_token_limited':bool(key_row and key_row[0] and int(key_row[0])==limited['quota']),'cancel_without_terminal_usage':cancel['saw_done'] is False,'cancelled_budget_owned_by_limited_key':held[5]==limited['id'],'unknown_reservation_positive':held[1]>0,'unknown_budget_row_stable_across_rollback':(held_after[0],held_after[1],held_after[2])==(held[0],held[1],held[2]),'unknown_usage_budget_preserved':held[0]=='unknown' and before['budget_reservations']==after['budget_reservations'],'all_new_b_rows_preserved':before==after,'snapshot_restore_calls_zero':report['snapshot_restore_calls']==0,'same_database_file':db_identity_before==db_identity_after,'all_protocols_before_and_after':all(len(report['phases'][p])==4 for p in ('a_before_upgrade','b_after_upgrade','a2_after_rollback')),'old_and_new_logs_present':complete['missing']==[] and complete['duplicate']==[],'tokens_reconcile':ledger['request_logs_tokens']==ledger['usage_daily_tokens'],'zero_duplicate_ids':ledger['duplicate_request_ids']==0,'admitted_stream_completed':holder['record']['terminal']=='completed','post_drain_rejected':rejected['status']==503,'reader_certificate_retained':after['schema']=={'user_version':9,'required_reader':7}}
        report['status']='passed' if all(report['checks'].values()) else 'failed'
    except BaseException as exc:
        report['status']='failed';report['error']=type(exc).__name__+': '+str(exc)[:500]
    finally:
        if holder:holder['thread'].join(5)
        for instance in reversed(cleanup):r4.stop_instance(instance,reason='final_cleanup')
        if entry:entry.stop()
        exp.timeline.close()
        if exp.provider:exp.provider.stop()
        report['isolated_root']=str(exp.root)
        (out/'lossless-rollback-report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({k:report.get(k) for k in ('status','checks','T_admission_ms','error')}),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
