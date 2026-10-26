"""Exercise K22 against a real, disposable systemd unit in the local VM."""
import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid


def main():
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('Disposable local VM only')
    cfg = json.loads((lab/'current-qa.json').read_text())
    source = Path(cfg['workspace']).resolve()
    if cfg['production_connected'] or not source.is_relative_to(lab/'snapshots'):
        raise RuntimeError('Unexpected source boundary')
    root = lab/'config-runs'/uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    unit = 'zhihui-qa-config-'+root.name+'.service'
    target = Path('/run/systemd/system')/unit
    dropdir = Path(str(target)+'.d')
    sleeper = root/'sleeper.py'
    sleeper.write_text('import time\ntime.sleep(600)\n')
    envfile = root/'fixture.env'
    envfile.write_text('WB_API_CONCURRENCY=9\nWB2API_KEY=synthetic-private-config-sentinel\n')
    os.chmod(envfile, 0o600)
    definition = root/'fixture.service'
    definition.write_text('[Unit]\nDescription=Disposable Zhihui configuration QA\n'
        '[Service]\nType=simple\nUser=gateway-lab\n'
        f'ExecStart=/usr/bin/python3 {sleeper} --port 23456 --host 127.0.0.1 --timeout-graceful-shutdown 30\n'
        f'EnvironmentFile={envfile}\nEnvironment=WB_GATEWAY_RATE_PER_MIN=100 WB_API_CONCURRENCY=7\n')
    first = root/'10-default.conf'
    last = root/'90-override.conf'
    first.write_text('[Service]\nEnvironment=WB_GATEWAY_RATE_PER_MIN=300\n')
    last.write_text('[Service]\nEnvironment=WB_GATEWAY_RATE_PER_MIN=600 WB_API_CONCURRENCY=11\n')
    result = {'schema':'zhihui-systemd-config-qa/v1','production_connected':False,
              'source_sha256':cfg['source_sha256'],'unit':unit,'checks':[],'status':'running'}

    def run(*args, check=True):
        return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)

    def collect():
        return run(cfg['python'],str(source/'ops/config/capture_effective.py'),
                   '--unit',unit,'--validator',str(source/'manager/server/configcontract.py'),check=False)

    try:
        run('sudo','-n','install','-m','644',str(definition),str(target))
        run('sudo','-n','install','-d','-m','755',str(dropdir))
        for path in (first,last):
            run('sudo','-n','install','-m','644',str(path),str(dropdir/path.name))
        run('sudo','-n','systemctl','daemon-reload')
        run('sudo','-n','systemctl','start',unit)
        capture = collect()
        assert capture.returncode == 0, 'actual service capture failed'
        snapshot = json.loads(capture.stdout)
        values = snapshot['configuration']['settings']
        assert values['WB_GATEWAY_RATE_PER_MIN']['value'] == 600
        # EnvironmentFile assignments override Environment even when the latter
        # appear in a lexically later drop-in. Read the actual process to prove it.
        assert values['WB_API_CONCURRENCY']['value'] == 9
        assert [Path(x['path']).name for x in snapshot['ordered_unit_sources']] == [unit,first.name,last.name]
        assert snapshot['launch_flags']['--port'] == '23456'
        assert snapshot['launch_flags']['--timeout-graceful-shutdown'] == '30'
        assert 'synthetic-private-config-sentinel' not in capture.stdout
        result['checks'] += ['real_unit_dropin_order_and_hashes',
                             'actual_process_environment_file_precedence',
                             'actual_allowlisted_launch_flags','secret_value_redacted']
        result['snapshot'] = snapshot
        envfile.write_text(envfile.read_text()+'WB_NOT_A_RECOGNIZED_SETTING=private-unknown-sentinel\n')
        run('sudo','-n','systemctl','restart',unit)
        rejected = collect()
        assert rejected.returncode == 2
        failure = json.loads(rejected.stdout)
        assert not failure['ok'] and any(x.get('code')=='unknown_variable' for x in failure['errors'])
        assert 'private-unknown-sentinel' not in rejected.stdout
        result['checks'].append('unknown_live_environment_rejected_without_value_disclosure')
        result['unknown_rejection'] = failure
        result['status'] = 'passed'
    finally:
        run('sudo','-n','systemctl','stop',unit,check=False)
        for name in (str(dropdir/first.name),str(dropdir/last.name),str(target)):
            run('sudo','-n','rm','-f','--',name,check=False)
        run('sudo','-n','rmdir','--',str(dropdir),check=False)
        run('sudo','-n','systemctl','daemon-reload')
        result['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('CONFIG_QA_RESULT '+str(root/'result.json'),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
