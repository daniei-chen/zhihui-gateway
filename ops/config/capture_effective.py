"""Read a systemd service's actual launch environment; never dump raw secrets.

Run with a service user's permission to read /proc/PID/environ. No mutation,
restart, daemon-reload, shell execution, DB imports or configuration loading.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess


PROPERTIES = ('MainPID','ActiveState','SubState','FragmentPath','DropInPaths','EnvironmentFiles')


def parse_properties(text):
    return dict(line.split('=',1) for line in text.splitlines() if '=' in line)


def capture(properties, proc_root, validator):
    pid = int(properties.get('MainPID','0'))
    if pid <= 0:raise ValueError('service_has_no_live_main_pid')
    proc = Path(proc_root) / str(pid)
    # Linux stat start ticks fence PID reuse across the read operation.
    first_stat = (proc/'stat').read_text()
    start_ticks = first_stat.rsplit(')',1)[1].split()[19]
    environment = {}
    for item in (proc/'environ').read_bytes().split(b'\0'):
        if b'=' in item:
            key,value = item.split(b'=',1)
            environment[key.decode('utf-8')]=value.decode('utf-8')
    snapshot = validator(environment)
    commands = [part.decode('utf-8') for part in (proc/'cmdline').read_bytes().split(b'\0') if part]
    flags = {}
    for index,word in enumerate(commands[:-1]):
        value=commands[index+1]
        if word in {'--port','--workers','--timeout-graceful-shutdown'}:
            if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',value):flags[word]=value
            else:flags[word]='<invalid-redacted>'
        elif word=='--host':
            flags[word]=value if re.fullmatch(r'[a-zA-Z0-9_.:\[\]-]+',value) else '<redacted>'
    if (proc/'stat').read_text().rsplit(')',1)[1].split()[19] != start_ticks:
        raise ValueError('main_pid_changed_during_capture')
    paths = [properties.get('FragmentPath','')] + shlex.split(properties.get('DropInPaths',''))
    files = []
    for position,name in enumerate(path for path in paths if path):
        path = Path(name)
        files.append({'order':position,'path':name,
                      'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'ok':True,'main_pid':pid,'start_ticks':start_ticks,
            'active_state':properties.get('ActiveState'), 'sub_state':properties.get('SubState'),
            'configuration':snapshot, 'launch_flags':flags,
            'ordered_unit_sources':files,
            'environment_files':properties.get('EnvironmentFiles',''),
            'provenance_note':'Values come from the running process, not a guessed merge. Unit sources are the manager-reported order; individual declaration winners are not inferred.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--unit',required=True)
    parser.add_argument('--validator',required=True,type=Path,
                        help='Installed service configcontract.py; imports stdlib only')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9@_.:-]*\.service',args.unit):
        parser.error('Expected an exact .service unit name')
    try:
        spec=importlib.util.spec_from_file_location('zhihui_config_validator',args.validator)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        properties=subprocess.run(['systemctl','show',args.unit,'--property='+','.join(PROPERTIES)],
            check=True,capture_output=True,text=True,timeout=15)
        report=capture(parse_properties(properties.stdout),'/proc',module.validate_environment)
        # Start ticks fence PID reuse; this second service lookup also fences a
        # restart/handoff where the old process is alive but no longer MainPID.
        after=subprocess.run(['systemctl','show',args.unit,'--property=MainPID'],
            check=True,capture_output=True,text=True,timeout=15)
        if int(parse_properties(after.stdout).get('MainPID','0')) != report['main_pid']:
            raise ValueError('service_main_pid_changed_during_capture')
        report['unit']=args.unit
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        errors=getattr(exc,'errors',None)
        # subprocess errors and private values are deliberately not echoed.
        print(json.dumps({'ok':False,'errors':errors or [{'code':'capture_failed','kind':type(exc).__name__}]}))
        return 2


if __name__=='__main__':raise SystemExit(main())
