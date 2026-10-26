"""Summarize captured, sanitized R4 output locally. No server/network operations."""
from pathlib import Path
import argparse
import collections
import datetime
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def result_text(record):
    text = '\n'.join(block.get('text', '') for block in record['result'].get('content', [])
                     if block.get('type') == 'text')
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    return obj.get('message', text) if isinstance(obj, dict) else text


def services(text):
    result = {}
    for block in re.split(r'\r?\n\s*\r?\n', text.strip()):
        fields = dict(line.rstrip('\r').split('=', 1) for line in block.splitlines() if '=' in line)
        if fields.get('Id'):
            result[fields['Id']] = fields
    return result


def fingerprints(text):
    return {match.group(2): match.group(1) for match in re.finditer(
        r'(?m)^([0-9a-f]{16})\s+(/[^\r\n]+)', text)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('capture_dir', type=Path)
    args = parser.parse_args()
    folder = args.capture_dir.resolve()
    if not folder.is_relative_to(ROOT / 'evidence/r4-preflight'):
        raise SystemExit('Output must stay in this task evidence/r4-preflight')
    packet = json.loads((folder / 'commands.json').read_text(encoding='utf-8'))
    commands = {item['id']: item for item in packet['records']}
    before = services(result_text(commands['services_before_light_nopager']))
    after = services(result_text(commands['services_after_light']))
    required = {'workbuddy-web.service', 'qtg.service', 'qoder2-engine.service', 'nginx.service'}
    health_before = result_text(commands['health_before_light'])
    health_after = result_text(commands['health_after_light'])
    safety = {'all_four_active_before': set(before) == required and all(v['ActiveState'] == 'active' for v in before.values()),
        'all_four_active_after': set(after) == required and all(v['ActiveState'] == 'active' for v in after.values()),
        'same_main_pid_and_start_time': set(before) == set(after) and all(
            all(before[name].get(prop) == after[name].get(prop) for prop in ('MainPID', 'ExecMainStartTimestamp'))
            for name in before),
        'manager_health_200_before': 'HTTP_STATUS=200' in health_before,
        'manager_health_200_after': 'HTTP_STATUS=200' in health_after}
    assert all(safety.values()), safety
    initial_fp = fingerprints(result_text(commands['small_file_fingerprints']))
    final_fp = fingerprints(result_text(commands['selected_fingerprints_after']))
    safety['selected_fingerprints_unchanged'] = bool(final_fp) and all(
        initial_fp.get(name) == value for name, value in final_fp.items())
    response = result_text(commands['health_headers'])
    version = re.search(r'(?im)^x-workbuddy-release:\s*([^\r\n]+)', response)
    db_sizes = {}
    for line in result_text(commands['file_metadata_initial']).splitlines():
        if line.startswith('/opt/workbuddy-manager/data/manager.db') and '|size=' in line:
            name, *fields = line.split('|')
            data = dict(field.split('=', 1) for field in fields if '=' in field)
            db_sizes[name] = data

    state = json.loads((ROOT / 'tasks/status.json').read_text(encoding='utf-8'))
    candidate = state['fact_baseline']['r2_candidate']
    artifact_dir = ROOT.parent / 'zhihui-linux-lab/cloud-init/artifacts'
    manifest_path = artifact_dir / (candidate['archive'] + '.manifest.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert digest(artifact_dir / candidate['archive']) == candidate['sha256']
    changed, missing = [], []
    for item in manifest['source_manifest']:
        path = ROOT / item['path']
        if not path.is_file():
            missing.append(item['path'])
        elif digest(path) != item['sha256']:
            changed.append(item['path'])
    out_root = ROOT / 'src/three-platform/web/out'
    out_items = [{'path': path.relative_to(out_root).as_posix(), 'bytes': path.stat().st_size,
                  'sha256': digest(path)} for path in sorted(out_root.rglob('*')) if path.is_file()]
    out_hash = hashlib.sha256(json.dumps(out_items, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    out_prefix = 'src/three-platform/web/out/'
    expected_out = {item['path'][len(out_prefix):]: item['sha256'] for item in manifest['source_manifest']
                    if item['path'].startswith(out_prefix)}
    current_out = {item['path']: item['sha256'] for item in out_items}
    build_id_path = ROOT / 'src/three-platform/web/.next/BUILD_ID'
    code_files = ['manager/server/instancelease.py', 'manager/server/tasklease.py',
                  'manager/server/migrations.py', 'manager/server/observability.py',
                  'qtg/app/qtg/manager_api.py', 'qtg/app/qtg/security.py']
    local = {'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'last_frozen_candidate': {key: candidate[key] for key in ('archive', 'sha256', 'file_count')},
        'last_archive_hash_verified': True,
        'compared_expected_files': len(manifest['source_manifest']),
        'current_changed_paths_relative_to_r2': changed,
        'current_missing_paths_relative_to_r2': missing,
        'note': 'R3 changes after R2 are expected; these differences do not imply the frozen archive changed.',
        'build_id_observed': build_id_path.read_text().strip() if build_id_path.is_file() else None,
        'out_fingerprint': out_hash, 'out_fingerprint_schema': 'sorted-relative-path-bytes-sha256-json/v1',
        'out_file_count': len(out_items), 'out_exactly_matches_r2_manifest': expected_out == current_out,
        'key_current_source_hashes': {name: digest(ROOT / name) for name in code_files},
        'candidate_frozen_by_this_review': False}
    baseline = {'schema': 'zhihui-r4-production-readonly-baseline/1',
        'summarized_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'collection_complete': False, 'preflight_verdict': 'NOT_READY_INCOMPLETE_BASELINE',
        'authority': packet['authority'], 'services_before': before, 'services_after': after,
        'safety': safety, 'manager_release_observed': version.group(1).strip() if version else None,
        'edge_anonymous_health_status': 401 if 'HTTP_STATUS=401' in result_text(commands['edge_health_verified_tls']) else None,
        'db_file_metadata_only': db_sizes,
        'unit_config_version_fingerprints': initial_fp,
        'secret_and_config_fingerprints_only': fingerprints(result_text(commands['secret_file_fingerprints'])),
        'categories': {
            'A_nginx': {'status': 'partial', 'collected': ['file metadata/fingerprints', 'one TLS-verified edge health response'],
                        'missing': ['nginx -T sanitized effective configuration', 'nginx -t']},
            'B_systemd': {'status': 'partial', 'collected': ['selected effective properties', 'unit/drop-in paths, metadata and fingerprints'],
                         'missing': ['sanitized full unit and launch arguments']},
            'C_versions_images': {'status': 'partial', 'collected': ['Manager response release header', 'version file fingerprint'],
                                  'missing': ['QTG/engine version', 'image digest and commit provenance']},
            'D_configuration_credentials': {'status': 'partial', 'collected': ['identified small credential/config file fingerprints and permissions'],
                                            'missing': ['effective runtime configuration with declaration provenance', 'full credential-consumer mapping']},
            'E_data_resources': {'status': 'partial', 'collected': ['Manager DB/WAL/SHM sizes', 'filesystem block counts', 'service cgroup memory/task metrics', 'host OS/architecture'],
                                 'missing': ['database schema/row counts/quick_check', 'log time range', 'full host memory/CPU topology']}},
        'blocking_reasons': ['systemd-run --user --scope rejected by connector systemd-unit-write rule; no SSH bypass',
            'scope exception only covers the explicitly authorized light metadata command set',
            'new R3-inclusive immutable release candidate and its full gate are not yet produced',
            'real accounts/notification/independent backup/deployment authorizations remain separate'],
        'findings': [
            {'id': 'PF01', 'fact': 'qoder2-engine PartOf=qtg.service still active',
             'implication': 'Manager/control-plane release plan must account for the actual coupling before any restart.'},
            {'id': 'PF02', 'fact': 'anonymous edge /api/healthz returned 401, direct Manager health returned 200',
             'implication': 'Clarify public/authenticated probe policy from effective nginx before accepting an external health gate.'},
            {'id': 'PF03', 'fact': 'Manager User empty (system service default root), QTG/Qoder User=root; Manager MemoryMax=infinity',
             'implication': 'Actual permissions/resource policy differs from candidate hardening goals; no production changes made.'}],
        'local_candidate_comparison': local,
        'raw_secret_contents_stored': False, 'production_config_database_modified': False,
        'services_restarted_or_reloaded': False, 'credentials_revoked': False,
        'limitations': ['A partial exit or timeout is preserved as such; missing values are unknown, not defaults.',
            'File metadata and fingerprints are point-in-time reads, not one atomic filesystem snapshot.',
            'The scope-guard rejection is a channel limitation, not proof of service failure.',
            'No SQL connection was opened; no immutable SQLite view was used.']}
    for record in packet['records']:
        text = '\n'.join(block.get('text', '') for block in record['result'].get('content', []) if block.get('type') == 'text')
        record['output_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    (folder / 'commands-index.json').write_text(json.dumps({'records': packet['records']}, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'baseline.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'local-candidate-comparison.json').write_text(json.dumps(local, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'folder': str(folder), 'verdict': baseline['preflight_verdict'], 'safety': safety,
        'release': baseline['manager_release_observed'], 'r2_changed_files': len(changed),
        'r2_missing_files': len(missing), 'out_matches_r2': local['out_exactly_matches_r2_manifest'],
        'out_fingerprint': out_hash}, ensure_ascii=False))


if __name__ == '__main__':
    main()
