"""Real HTTP K41 at UNOVERRIDDEN 10s/30s defaults; disposable local clone only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
from r3l2_handoff import HandoffExperiment, Instance, LAUNCHER


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--repo', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--duration', type=int, default=90)
    p.add_argument('--write-delay', type=float, default=0, help='Explicit synthetic slow-storage fault; normal K41 uses 0')
    args=p.parse_args()
    repo,out=Path(args.repo).resolve(),Path(args.out).resolve()
    lab=Path('/home/gateway-lab/lab')
    local_vm=socket.gethostname()=='zhihui-local-lab' and Path.home()==lab.parent and repo.is_relative_to(lab/'snapshots') and out.is_relative_to(lab)
    if out.exists() or args.duration < 65 or not 0 <= args.write_delay <= .05:
        raise RuntimeError('Unique evidence and >=65s required')
    if not local_vm:
        if not out.is_relative_to(repo/'evidence') or not (repo.parent/'job.json').is_file():
            raise RuntimeError('Requires an explicit isolated closeout job, not the primary tree')
        job=json.loads((repo.parent/'job.json').read_text())
        if Path(job['workspace']).resolve()!=repo or job.get('production_modified') is not False:
            raise RuntimeError('Isolated job boundary mismatch')
    exp=HandoffExperiment(argparse.Namespace(repo=str(repo),out=str(out),chunk_delay=.05,chunk_count=40,instance_ttl=30))
    report={'status':'running','production_connected':False,'heartbeat_sec':10,'ttl_sec':30,'manual_heartbeat_calls':0,'samples':[],'file_write_delay_sec':args.write_delay}
    instance,driver=None,None
    try:
        exp.prepare()
        for name in ('ZH_INSTANCE_HEARTBEAT_SEC','ZH_INSTANCE_LEASE_TTL_SEC'):
            exp.env.pop(name,None)
        # Bounded synthetic slow-storage fault: actual atomic write still executes.
        hook="from server.services import tencent\nimport time\n_atomic=tencent._atomic_write_json\ndef delayed_atomic(*a,**kw):\n    time.sleep(.009)\n    return _atomic(*a,**kw)\ntencent._atomic_write_json=delayed_atomic\n"
        hook=hook.replace('time.sleep(.009)', 'time.sleep('+repr(args.write_delay)+')' if args.write_delay else 'pass')
        launcher=LAUNCHER.replace('from server.serve import main',hook+'from server.serve import main')
        # Local synthetic admin only; production KDF and auth code are untouched.
        launcher=launcher.replace("security.make_hash('r3l2-synthetic-password')","security.make_hash('r3l2-synthetic-password', iterations=1000)")
        report['synthetic_password_kdf_iterations']=1000
        (exp.root/'launch_manager.py').write_text(launcher,encoding='utf8')
        instance=Instance(exp,'default-import')
        if not instance.start():raise AssertionError('Manager not ready')
        origin=time.monotonic()
        with (out/'k41.stdout.log').open('w',encoding='utf8') as log:
            driver=subprocess.Popen([sys.executable,'-B','-X','utf8',str(repo/'tests/linux_lab/k41_export_import.py'),'--manager',instance.url,'--admin-user','r3l2-admin','--admin-pass','r3l2-synthetic-password','--json',str(out/'k41.json')],env=exp.env,stdout=log,stderr=subprocess.STDOUT)
            expiries=set()
            import httpx
            with httpx.Client(base_url=instance.url,timeout=5,trust_env=False) as client:
                while time.monotonic()-origin < args.duration or driver.poll() is None:
                    if time.monotonic()-origin>220:raise AssertionError('K41 exceeded bounded test window')
                    row=exp.instance_row(instance.info['instance_id'])
                    ready=client.get('/api/readyz').status_code
                    sample={'elapsed_sec':round(time.monotonic()-origin,3),'readyz':ready,'lease_until':(row or {}).get('lease_until'),'role':(row or {}).get('role')}
                    report['samples'].append(sample)
                    if ready!=200 or not row or row['lease_until']<=int(time.time()):raise AssertionError('Default lease lost during import')
                    expiries.add(row['lease_until'])
                    time.sleep(.5)
        report['elapsed_sec']=round(time.monotonic()-origin,3)
        report['distinct_expiries']=len(expiries)
        if driver.returncode!=0 or not (out/'k41.json').is_file():
            report['k41']={'exit_code':driver.returncode,'complete_report':False}
            raise AssertionError('K41 driver failed before report; see k41.stdout.log')
        k41=json.loads((out/'k41.json').read_text(encoding='utf8'))
        report['k41']={'passed':k41['passed'],'failed':k41['failed'],'exit_code':driver.returncode}
        report['checks']={'duration_at_least_65s':report['elapsed_sec']>=65,'at_least_six_automatic_renewals':len(expiries)>=7,'k41_all_cases':driver.returncode==0 and k41['failed']==0,'all_ready':all(s['readyz']==200 for s in report['samples'])}
        report['status']='passed' if all(report['checks'].values()) else 'failed'
        report['source_sha256']={str(x.relative_to(repo)):hashlib.sha256(x.read_bytes()).hexdigest() for x in [repo/'manager/server/instancelease.py',repo/'manager/server/instance_runtime.py',repo/'manager/server/routers/accounts.py']}
    except BaseException as exc:
        report['status']='failed';report['error']=type(exc).__name__+': '+str(exc)
    finally:
        if driver is not None and driver.poll() is None:driver.terminate();driver.wait(10)
        if instance is not None:instance.stop(timeout=15)
        exp.timeline.close()
        if exp.provider is not None:exp.provider.stop()
        report['isolated_root']=str(exp.root)
        (out/'default-ttl-import-report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({k:report.get(k) for k in ('status','elapsed_sec','distinct_expiries','k41','checks','error')}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
