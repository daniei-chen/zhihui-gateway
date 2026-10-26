"""Controlled live SSE crossing real SIGTERM; isolated disposable VM only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from unittest.mock import patch
from r3l2_handoff import HandoffExperiment, Instance, LAUNCHER


def scenario(repo, out, grace):
    exp=HandoffExperiment(argparse.Namespace(repo=str(repo),out=str(out),chunk_delay=0,chunk_count=4,instance_ttl=30))
    sys.path.insert(0,str(repo/'tests'))
    from fake_provider.server import FakeProvider
    gate, upstream_held = threading.Event(), threading.Event()
    original=FakeProvider._make_handler
    def factory(provider):
        Parent=original(provider)
        class Gated(Parent):
            def _write_chunk(self,data):
                super()._write_chunk(data)
                if b'"content"' in data and not getattr(self,'_gate_held',False):
                    self._gate_held=True
                    upstream_held.set()
                    if not gate.wait(90):raise TimeoutError('controlled upstream gate timed out')
        return Gated
    record={'graceful_sec':grace,'production_connected':False,'status':'running'}
    instance,holder=None,None
    try:
        with patch.object(FakeProvider,'_make_handler',factory):exp.prepare()
        exp.env['ZH_INSTANCE_HEARTBEAT_SEC']='10'
        exp.env['WB_DRAIN_TIMEOUT_SEC']=str(grace)
        launcher=LAUNCHER.replace("'--timeout-graceful-shutdown', '45'", "'--timeout-graceful-shutdown', '"+str(grace)+"'")
        # Synthetic-only fixture KDF: shutdown experiment is not a KDF benchmark.
        launcher=launcher.replace("security.make_hash('r3l2-synthetic-password')","security.make_hash('r3l2-synthetic-password', iterations=1000)")
        record['synthetic_password_kdf_iterations']=1000
        (exp.root/'launch_manager.py').write_text(launcher,encoding='utf8')
        instance=Instance(exp,'grace-'+str(grace))
        if not instance.start():raise AssertionError('Manager not ready')
        holder=exp.spawn_stream(instance,'chat','workbuddy',label='must-cross-SIGTERM',keys=instance.info['keys'])
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if upstream_held.is_set() and holder.get('progress',{}).get('frames',0)>0:break
            if not holder['thread'].is_alive():raise AssertionError('Stream finished before signal; invalid experiment')
            time.sleep(.02)
        else:raise AssertionError('No live client frame at provider gate')
        if not holder['thread'].is_alive() or gate.is_set():raise AssertionError('No held stream at signal')
        record['first_frame_observed_ms']=exp.timeline.rel_ms()
        record['live_at_signal']=True
        signal_ms=exp.timeline.rel_ms()
        instance.proc.send_signal(signal.SIGTERM)
        record['signal_ms']=signal_ms
        # Keep completion impossible through the short 3s shutdown budget.
        until=time.monotonic()+8
        while time.monotonic()<until:time.sleep(.1)
        record['provider_release_ms']=exp.timeline.rel_ms()
        gate.set()
        holder['thread'].join(60)
        if holder['thread'].is_alive():raise AssertionError('Client did not finish within bounded window')
        result=holder.get('stream') or {}
        record['stream']=result
        instance.proc.wait(65)
        record['returncode']=instance.proc.returncode
        completed=result.get('terminal')=='completed'
        record['checks']={'first_frame_before_signal':record['first_frame_observed_ms']<=signal_ms,'provider_completes_after_signal':record['provider_release_ms']>signal_ms+7000,'live_at_signal':record['live_at_signal'],'stream_ends_after_signal':result.get('t_end_ms',0)>signal_ms,'http_accepted_before_signal':result.get('status')==200,'observed_expected_shutdown': (result.get('terminal')=='stream_cut' if grace==3 else completed and result.get('t_end_ms',0)>=record['provider_release_ms']),'manager_exited':instance.proc.poll() is not None}
        record['status']='passed' if all(record['checks'].values()) else 'failed'
    except BaseException as exc:
        record['status']='failed';record['error']=type(exc).__name__+': '+str(exc)
    finally:
        gate.set()
        if holder:holder['thread'].join(5)
        if instance:instance.stop(timeout=10)
        exp.timeline.close()
        if exp.provider:exp.provider.stop()
        record['isolated_root']=str(exp.root)
        (out/'cross-window-report.json').write_text(json.dumps(record,indent=2),encoding='utf8')
    return record


def main():
    p=argparse.ArgumentParser();p.add_argument('--repo',required=True);p.add_argument('--out',required=True);args=p.parse_args()
    repo,out=Path(args.repo).resolve(),Path(args.out).resolve();lab=Path('/home/gateway-lab/lab')
    if socket.gethostname()!='zhihui-local-lab' or not repo.is_relative_to(lab/'snapshots') or not out.is_relative_to(lab) or out.exists():raise RuntimeError('Disposable local VM boundary only; unique evidence')
    out.mkdir(parents=True)
    records=[scenario(repo,out/('grace-'+str(g)),g) for g in (3,45)]
    report={'production_connected':False,'status':'passed' if all(x['status']=='passed' for x in records) else 'failed','scenarios':records,'source_sha256':{str(f.relative_to(repo)):hashlib.sha256(f.read_bytes()).hexdigest() for f in (repo/'manager/server/serve.py',repo/'manager/server/instance_runtime.py')}}
    (out/'summary.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({'status':report['status'],'scenarios':[{k:r.get(k) for k in ('graceful_sec','status','checks','error')} for r in records]}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
