"""Release regressions for the independent audit's accounting findings."""
import json
import importlib.util
import io
import tarfile
import time
from pathlib import Path

import pytest

from server.tests.audit_fixtures import state, provider, portal_key


def test_used_channel_loss_and_empty_replacement_remain_blocked(state):
    from server import ledgerreplay, portalwallet
    entry=ledgerreplay.build_entry({'request_id':'audit-lost','key_id':1,'credit':2})
    assert ledgerreplay.spill(entry)
    original=ledgerreplay.spool_dir()
    detached=original.with_name('preserved-original')
    original.rename(detached)
    for replace in (False,True):
        if replace:original.mkdir()
        for _ in range(3):
            result=ledgerreplay.channel_state(force_probe=True)
            assert result['state']=='degraded'
            assert ledgerreplay.admission_block()['blocked']
            assert portalwallet.replay_problem()
        assert original.exists() is replace  # probe never fabricates a replacement
    original.rmdir()  # verified-empty synthetic replacement only
    detached.rename(original)
    assert ledgerreplay.channel_state(force_probe=True)['state']=='ok'
    assert not ledgerreplay.admission_block()['blocked']
    assert ledgerreplay.backlog()==1


def test_missing_registry_and_receipts_never_recreate_a_used_channel(state):
    from server import config,ledgerreplay,spoolidentity
    entry=ledgerreplay.build_entry({'request_id':'audit-missing-registration','key_id':1,'credit':2})
    assert ledgerreplay.spill(entry)
    original=ledgerreplay.spool_dir()
    original.rename(original.with_name('preserved-registered-receipts'))
    (Path(config.DATA_DIR)/spoolidentity.REGISTRY).unlink()
    for _ in range(3):
        assert ledgerreplay.admission_block()['blocked']
        assert ledgerreplay.channel_state(force_probe=True)['state']=='degraded'
        assert not original.exists()
        assert not (Path(config.DATA_DIR)/spoolidentity.REGISTRY).exists()


def test_explicit_offline_adoption_cannot_override_existing_channel_identity(state):
    from server import ledgerreplay,spoolidentity
    assert ledgerreplay.spill(ledgerreplay.build_entry({'request_id':'audit-adoption-refused','credit':2}))
    original=ledgerreplay.spool_dir()
    original.rename(original.with_name('preserved-original-channel'))
    original.mkdir()
    with pytest.raises(ValueError,match='already registered'):
        spoolidentity.adopt_legacy(original,spoolidentity.inventory(original),'synthetic-operator','synthetic recovery evidence')
    assert ledgerreplay.admission_block()['blocked']


def test_empty_legacy_inventory_is_not_offline_recovery_proof(state):
    from server import config,spoolidentity
    directory=state/'empty-legacy'
    directory.mkdir()
    (state/'.spool-used').write_text('synthetic historical use')
    with pytest.raises(ValueError,match='empty legacy inventory'):
        spoolidentity.adopt_legacy(directory,spoolidentity.inventory(directory),'synthetic-operator','synthetic recovery evidence')
    assert not (Path(config.DATA_DIR)/spoolidentity.REGISTRY).exists()
    assert not (directory/spoolidentity.IDENTITY).exists()


def test_legacy_channel_requires_offline_verified_inventory(state):
    from server import ledgerreplay, spoolidentity
    channel=ledgerreplay.spool_dir()
    channel.mkdir()
    (state/'.spool-used').write_text('legacy')
    (channel/'preserved.json').write_text('{"legacy":"retained"}')
    assert ledgerreplay.channel_state(force_probe=True)['state']=='degraded'
    digest=spoolidentity.inventory(channel)
    with pytest.raises(ValueError,match='inventory'):
        spoolidentity.adopt_legacy(channel,'wrong-digest','operator-audit','verified complete local recovery backup')
    result=spoolidentity.adopt_legacy(channel,digest,'operator-audit','verified complete local recovery backup')
    assert result['ok']
    assert (channel/'preserved.json').read_text()=='{"legacy":"retained"}'
    assert ledgerreplay.channel_state(force_probe=True)['state']=='ok'


@pytest.mark.parametrize('function',['backlog','dead_letters'])
def test_unreadable_channel_counts_are_not_treated_as_empty(state,monkeypatch,function):
    from server import ledgerreplay
    monkeypatch.setattr(ledgerreplay,function,lambda:-1)
    assert ledgerreplay.channel_state(force_probe=True)['state']=='degraded'
    assert ledgerreplay.admission_block()['blocked']


@pytest.mark.parametrize('credit,expected',[(0,0),(0.123456,123456),(0.1234567,123457),
                                         ('0.1234565',123456),('0.1234575',123458),('0.123457499999999999999',123457)])
def test_supplier_rounding_matches_authoritative_ledger(credit,expected):
    from server import ledger,portalwallet
    assert portalwallet.supplier_units(credit)==ledger.units(credit)==expected


@pytest.mark.parametrize('credit',[None,True,-1,float('inf'),float('nan'),'9e999',{},[]])
def test_invalid_supplier_cost_remains_unknown(credit):
    from server import portalwallet
    assert portalwallet.supplier_units(credit) is None


def test_operator_money_precision_is_still_strict():
    from server import portalstore
    with pytest.raises(ValueError):portalstore.units('0.1234567')


def test_metadata_endpoint_is_nonbillable_but_missing_generation_usage_stays_unknown(state):
    from server import db,metering
    for rid,path in [('meta','/v1/key'),('generation','/v1/chat/completions')]:
        db.add_request_log(ts=int(time.time()),request_id=rid,key_id=1,status=200,endpoint=path,
            protocol='openai',terminal_state='completed',usage_source='absent',usage_complete=0)
    result=metering.reconcile(db.connect())
    assert result['spend_classes'][metering.SPEND_NOT_BILLABLE]==1
    assert result['spend_classes'][metering.SPEND_UNKNOWN]==1
    finding=result['findings']['zero_token_completed']
    assert finding['count']==1
    assert all(row['request_id']!='meta' for row in finding['samples'])


@pytest.mark.parametrize('manual',[False,True])
@pytest.mark.parametrize('receipt',[False,True])
def test_zero_user_charge_does_not_assert_unknown_supplier_cost_is_zero(state,monkeypatch,manual,receipt):
    from server import budget,db,keysvc,observability,portalstore as store,portalwallet
    from server.routers import gateway
    made=portal_key();key=keysvc.resolve(made['key'])
    audit=observability.RequestAudit('audit-unknown-cost','/v1/chat/completions','POST','openai',time.monotonic())
    token=observability.current.set(audit)
    try:
        budget.prepare(key,{'model':'deepseek-v4-flash','messages':[]});budget.forwarded()
        audit.terminal_state='client_cancelled'
        if receipt:
            gateway._record(key,'127.0.0.1','deepseek-v4-flash','cn:deepseek-v4-flash',200,0,0,1,None,'synthetic cancellation',True,usage={})
        else:
            db.add_request_log(ts=int(time.time()),request_id=audit.request_id,key_id=key['id'],status=499,terminal_state='client_cancelled')
        budget.finish(audit)
    finally:observability.current.reset(token)
    monkeypatch.setenv('WB_PORTAL_AUTO_CLOSE_CLIENT_CANCEL','1')
    monkeypatch.setenv('WB_PORTAL_CLIENT_CANCEL_GRACE_SEC','0')
    if manual:
        portalwallet.resolve_unknown(audit.request_id,0,'synthetic-operator','synthetic user fee waived','synthetic evidence supplier fee still unknown')
    else:assert portalwallet.auto_close_client_cancelled()==1
    assert portalwallet.auto_close_client_cancelled()==0
    with store.transaction() as conn:
        assert conn.execute('SELECT charged_units FROM portal_spends WHERE request_id=?',(audit.request_id,)).fetchone()[0]==0
        assert not conn.execute('SELECT 1 FROM portal_settlements WHERE request_id=?',(audit.request_id,)).fetchone()
        assert conn.execute('SELECT charged_units FROM portal_unknown_supplier_costs WHERE request_id=?',(audit.request_id,)).fetchone()[0]==0
    assert portalwallet.platform_costs_page()['unknown_supplier_costs']==1
    assert portalwallet.platform_costs_page()['cost_complete'] is False


@pytest.mark.parametrize('credit,expected',[(0.1234567,123457),('0.1000000000000000055511151231257827',100000),('0.123456499999999999999999',123456),('1e-7',0)])
def test_precision_settles_once_and_next_user_request_works(state,provider,monkeypatch,credit,expected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server import config,db,observability,portalstore as store,portalwallet
    from server.routers import gateway
    made=portal_key()
    provider.credit=credit
    monkeypatch.setattr(config,'WB2API_BASE',f'http://127.0.0.1:{provider.server_port}')
    app=FastAPI();app.include_router(gateway.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    with TestClient(app) as client:
        first=client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+made['key']},
             json={'model':'deepseek-v4-flash','messages':[{'role':'user','content':'hi'}]})
        assert first.status_code==200,first.text
        request_id=first.headers['x-request-id']
        assert json.loads(db.query_one('SELECT payload FROM portal_spend_receipts WHERE request_id=?',(request_id,))[0])['amount_units']==expected
        assert db.query_one('SELECT used_credit_units FROM api_keys WHERE prefix=?',(made['key'][:12],))[0]==expected
        for _ in range(3):portalwallet.recover()
        with store.transaction() as conn:
            row=conn.execute('SELECT * FROM portal_spends WHERE request_id=?',(request_id,)).fetchone()
            assert row['state']=='settled' and row['charged_units']==expected
            assert conn.execute('SELECT supplier_units FROM portal_settlements WHERE request_id=?',(request_id,)).fetchone()[0]==expected
        second=client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+made['key']},
             json={'model':'deepseek-v4-flash','messages':[{'role':'user','content':'again'}]})
        assert second.status_code==200,second.text
        assert provider.calls==2


def test_reset_epoch_survives_reconcile_without_recharging_history(state):
    from server import budget, db, keysvc, observability
    from server.routers import gateway

    key = keysvc.create_key('synthetic ordinary key', quota_credit=2)
    audit = observability.RequestAudit('synthetic-reset', '/v1/chat/completions', 'POST',
                                       'openai', time.monotonic())
    token = observability.current.set(audit)
    try:
        budget.prepare(key, {'model': 'fake-model', 'messages': []})
        budget.forwarded()
        gateway._record(key, '127.0.0.1', 'fake-model', 'fake-model', 200, 17, 5, 1, None, None,
                        False, usage={'prompt_tokens': 17, 'completion_tokens': 5, 'credit': 2.0})
    finally:
        observability.current.reset(token)
    assert db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?', (key['id'],))[0] == 2_000_000
    assert budget.unresolved(key['id']) == []
    assert keysvc.reset_usage(key['id'])
    assert db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?', (key['id'],))[0] == 0
    result = db.reconcile_credit_from_events('admin', 'synthetic reset verification', apply=True)
    assert result['applied_units'] == 0
    assert db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?', (key['id'],))[0] == 0
    assert db.query_one('SELECT COUNT(*) FROM usage_events WHERE key_id=?',(key['id'],))[0] == 1
    assert db.query_one('SELECT event_units_before FROM usage_reset_events WHERE key_id=?',(key['id'],))[0] == 2_000_000

    # Fresh post-reset spending is reconciled; a second reset creates a new
    # immutable epoch instead of reusing the previous adjustment identifier.
    from server import ledger
    for index in (1,2):
        ledger.record(f'audit-new-spend-{index}',key['id'],'synthetic','cn',1,1,1.0,True,'completed','synthetic')
        result=db.reconcile_credit_from_events('admin','synthetic post-reset verification',apply=True)
        assert result['applied_units']==1_000_000
        assert db.query_one('SELECT used_credit_units FROM api_keys WHERE id=?',(key['id'],))[0]==1_000_000
        assert keysvc.reset_usage(key['id'])
    assert db.query_one('SELECT COUNT(*) FROM adjustment_events WHERE key_id=?',(key['id'],))[0]==2


@pytest.mark.parametrize('complete',[False,True])
def test_cancellation_releases_user_balance_and_retains_known_supplier_cost(state, monkeypatch,complete):
    from server import budget, db, keysvc, observability, portalstore as store, portalwallet
    from server.routers import gateway

    made = portal_key()
    key = keysvc.resolve(made['key'])
    audit = observability.RequestAudit('synthetic-cancel-known', '/v1/chat/completions',
                                       'POST', 'openai', time.monotonic())
    token = observability.current.set(audit)
    try:
        budget.prepare(key, {'model': 'deepseek-v4-flash', 'messages': []})
        budget.forwarded()
        audit.terminal_state = 'client_cancelled'
        gateway._record(key, '127.0.0.1', 'deepseek-v4-flash', 'cn:deepseek-v4-flash',
                        200, 17, 5, 1, None, 'synthetic client cancellation after usage', True,
                        usage={'prompt_tokens': 17, 'credit': 0.123456,**({'completion_tokens':5} if complete else {})})
        budget.finish(audit)
    finally:
        observability.current.reset(token)
    log = db.query_one('SELECT usage_complete,credit,terminal_state FROM request_logs WHERE request_id=?',
                       (audit.request_id,))
    assert log['usage_complete'] == int(complete) and log['credit'] == 0.123456 and log['terminal_state'] == 'client_cancelled'
    monkeypatch.setenv('WB_PORTAL_AUTO_CLOSE_CLIENT_CANCEL', '1')
    monkeypatch.setenv('WB_PORTAL_CLIENT_CANCEL_GRACE_SEC', '0')
    assert portalwallet.auto_close_client_cancelled() == 1
    with store.transaction() as conn:
        settlement = conn.execute('SELECT * FROM portal_settlements WHERE request_id=?',
                                   (audit.request_id,)).fetchone()
        assert settlement['supplier_units'] == 123456
        assert settlement['charged_units'] == 0 and settlement['platform_units'] == 123456
    assert portalwallet.platform_costs_page()['platform_units'] == 123456
    # Paired recovery must use supplier_units even if token completeness kept
    # the user's amount_units unknown. The snapshot contains synthetic data only.
    from server import portalrecovery
    (state/'users.json').write_text('{}')
    portalrecovery.create_paired_snapshot(main_db=state/'manager.db',portal_db=state/'user-portal.sqlite3',
        users=state/'users.json',auths=state/'auths',isolated=state/'user-portal-auths',
        spool=state/'ledger-spool',destination=state.parent/'paired-cancel-cost')


def test_real_asgi_disconnect_preserves_supplier_cost(state, provider, monkeypatch):
    import asyncio
    from fastapi import FastAPI
    from starlette.requests import ClientDisconnect
    from server import config, db, observability, portalstore as store, portalwallet
    from server.routers import gateway

    provider.credit = 0.123456
    made = portal_key()
    monkeypatch.setattr(config, 'WB2API_BASE', f'http://127.0.0.1:{provider.server_port}')
    monkeypatch.setenv('WB_SSE_HEARTBEAT_SECONDS', '0')
    app = FastAPI()
    app.include_router(gateway.router)
    app.add_middleware(observability.RequestAuditMiddleware)
    raw = json.dumps({'model': 'deepseek-v4-flash', 'stream': True,
                      'messages': [{'role': 'user', 'content': 'hi'}]}).encode()
    sent_headers = []
    delivered = []

    async def run():
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': raw, 'more_body': False}
            await asyncio.Event().wait()

        async def send(message):
            if message['type'] == 'http.response.start':
                sent_headers.extend(message['headers'])
            if message['type'] == 'http.response.body' and message.get('body'):
                delivered.append(message['body'])
                raise OSError('synthetic downstream disconnected during terminal usage frame')

        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                 'path': '/v1/chat/completions', 'raw_path': b'/v1/chat/completions',
                 'root_path': '', 'query_string': b'', 'client': ('127.0.0.1', 10000),
                 'server': ('127.0.0.1', 80),
                 'headers': [(b'host', b'127.0.0.1'), (b'content-type', b'application/json'),
                             (b'content-length', str(len(raw)).encode()),
                             (b'authorization', ('Bearer ' + made['key']).encode())]}
        try:
            await app(scope, receive, send)
        except (OSError, ClientDisconnect):
            pass

    asyncio.run(run())
    assert delivered and b'usage' in delivered[0]
    rid = dict(sent_headers)[b'x-request-id'].decode()
    log = db.query_one('SELECT usage_complete,credit,terminal_state FROM request_logs WHERE request_id=?', (rid,))
    assert log['usage_complete'] == 1 and log['credit'] == 0.123456
    assert log['terminal_state'] == 'client_cancelled'
    monkeypatch.setenv('WB_PORTAL_AUTO_CLOSE_CLIENT_CANCEL', '1')
    monkeypatch.setenv('WB_PORTAL_CLIENT_CANCEL_GRACE_SEC', '0')
    assert portalwallet.auto_close_client_cancelled() == 1
    with store.transaction() as conn:
        settlement = conn.execute('SELECT * FROM portal_settlements WHERE request_id=?', (rid,)).fetchone()
        assert settlement['supplier_units'] == 123456 and settlement['platform_units'] == 123456


@pytest.mark.parametrize('matching',[False,True])
@pytest.mark.parametrize('sqlite_mode',['ok','broken','skip','empty'])
def test_restore_digest_mismatch_prevents_sidecar_write_and_verified_status(tmp_path, monkeypatch, matching, sqlite_mode):
    import sqlite3
    candidate = Path(__file__).resolve().parents[2]
    path = candidate / 'deploy' / 'restore_drill.py'
    spec = importlib.util.spec_from_file_location('synthetic_restore_drill', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    cipher = b'synthetic-cipher-no-secrets'
    (snapshot / 'cipher.part').write_bytes(cipher)
    import hashlib
    (snapshot / 'snapshot.json').write_text(json.dumps({
        'parts': [{'name': 'cipher.part'}], 'cipher_sha256': hashlib.sha256(cipher).hexdigest(),
        'archive_sha256': '0' * 64}))
    keyfile = tmp_path / 'synthetic-not-a-key'
    keyfile.write_text('synthetic placeholder; no real credential')
    database = tmp_path / 'synthetic.sqlite3'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE synthetic(id INTEGER PRIMARY KEY)')
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode='w:gz') as archive:
        raw = json.dumps({'files': {}, 'sqlite_snapshots': [{'name': 'manager/data/manager.db'}]}).encode()
        info = tarfile.TarInfo('recovery-manifest.json')
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
        if sqlite_mode!='empty':
            raw = database.read_bytes()
            info = tarfile.TarInfo('manager/data/manager.db')
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))

    if matching:
        meta=json.loads((snapshot/'snapshot.json').read_text())
        meta['archive_sha256']=hashlib.sha256(archive_bytes.getvalue()).hexdigest()
        (snapshot/'snapshot.json').write_text(json.dumps(meta))

    class FakeProcess:
        def __init__(self, args, **kwargs):
            self.returncode = 0
            self.stdout = io.BytesIO(cipher) if args[0] == 'cat' else None
            if args[0] == 'age':
                kwargs['stdout'].write(archive_bytes.getvalue())
                kwargs['stdout'].close()

        def communicate(self):
            return b'', b''

        def wait(self):
            return self.returncode

    monkeypatch.setattr(module, 'KEY', keyfile)
    monkeypatch.setattr(module, 'WORK', tmp_path / 'work')
    monkeypatch.setattr(module.subprocess, 'Popen', FakeProcess)
    if sqlite_mode=='broken':
        class BrokenDatabase:
            def execute(self,*a):return self
            def fetchone(self):return ('synthetic corruption',)
            def close(self):pass
        monkeypatch.setattr(module.sqlite3,'connect',lambda *a,**kw:BrokenDatabase())
    result=module.main([str(snapshot),'--restore-sidecar']+(['--skip-sqlite'] if sqlite_mode=='skip' else []))
    report = json.loads((tmp_path / 'work' / 'drill-report.json').read_text())
    successful=matching and sqlite_mode!='broken'
    assert (result==0) is successful
    assert report['result']==('sampled' if successful else 'failed')
    assert next(item for item in report['steps'] if item['step'] == 'archive_sha256')['ok'] is matching
    assert (snapshot/'recovery-manifest.json').exists() is matching
    assert report['sqlite_skipped'] is (sqlite_mode=='skip')
    if matching and sqlite_mode=='broken':assert report['error']=='sqlite_integrity_failed'
    if successful:
        assert report['verification_scope']==('archive-and-sampled-sqlite' if sqlite_mode=='ok' else 'archive-only')
        if sqlite_mode=='ok':assert report['sqlite_checks'][0]['integrity']=='ok'
        else:assert report['sqlite_checks']==[]


def test_orphaned_identity_without_registry_never_gets_automatically_adopted(state):
    from server import ledgerreplay,spoolidentity
    directory=ledgerreplay.spool_dir();directory.mkdir()
    (directory/spoolidentity.IDENTITY).write_text(json.dumps({'schema':1,'id':'a'*32}))
    assert not spoolidentity.check(directory,create=True)['ok']
    assert not (state/spoolidentity.REGISTRY).exists()


def test_nonempty_channel_without_any_registration_is_never_auto_adopted(state):
    from server import ledgerreplay,spoolidentity
    directory=ledgerreplay.spool_dir();directory.mkdir()
    original=directory/'pending-synthetic.json';original.write_text('{}')
    assert ledgerreplay.channel_state(force_probe=True)['state']=='degraded'
    assert ledgerreplay.admission_block()['blocked']
    assert original.read_text()=='{}'
    assert not (state/spoolidentity.REGISTRY).exists()
    assert not (directory/spoolidentity.IDENTITY).exists()


def test_unresolved_dead_receipt_blocks_usage_reset(state):
    from server import keysvc,ledgerreplay
    key=keysvc.create_key('synthetic dead receipt')
    pending=Path(ledgerreplay.spill(ledgerreplay.build_entry({'request_id':'synthetic-dead-reset','key_id':key['id'],'credit':1})))
    dead=pending.parent/'dead';dead.mkdir()
    pending.rename(dead/pending.name)
    assert ledgerreplay.backlog()==0 and ledgerreplay.dead_letters()==1
    with pytest.raises(ValueError,match='未决请求或费用回执'):
        keysvc.reset_usage(key['id'])


def test_metadata_record_marks_usage_not_applicable(state):
    from server import db,observability
    from server.routers import gateway
    audit=observability.RequestAudit('synthetic-meta-record','/v1/key','GET','openai',time.monotonic())
    token=observability.current.set(audit)
    try:gateway._record(None,'127.0.0.1','','',200,0,0,1,None,None,False)
    finally:observability.current.reset(token)
    assert db.query_one('SELECT usage_source FROM request_logs WHERE request_id=?',(audit.request_id,))[0]=='not_applicable'


@pytest.mark.parametrize('raw',[{}, {'prompt_tokens':0,'completion_tokens':0,'credit':0}])
def test_metadata_usage_shapes_never_turn_into_generation_spend(raw):
    from server import metering
    usage=metering.parse_usage(raw)
    assert metering.spend_class(terminal_state='completed',usage=usage,credit=usage.credit,endpoint='/v1/key')==metering.SPEND_NOT_BILLABLE
