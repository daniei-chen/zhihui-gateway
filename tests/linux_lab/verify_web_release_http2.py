"""K14 drill v2: full release flow against real Nginx + verified TLS with TWO real builds.

Extends verify_web_release_http.py with the 2026-10-24 publisher fixes:
  - rollback-target integrity precheck (corrupted current => refuse, exit 6)
  - LINK_MODE=none preview install + idempotent run completing the switch
Also records the known K33 gap (old hash chunks not reachable via current root).

Restricted to the disposable local VM. No production host, paths or credentials.
Keeps the run root and logs as evidence; stops only its own Nginx instance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import uuid


def main() -> int:
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('This check may run only in the disposable local VM')
    parser = argparse.ArgumentParser()
    parser.add_argument('--script', required=True)
    parser.add_argument('--port', type=int, default=443,
                        help='演练 nginx 监听端口（fixture 占用 443 时用高位端口）')
    parser.add_argument('--old-export', required=True,
                        help='previous real export (out dir) to seed as the running version')
    args = parser.parse_args()
    script = Path(args.script).resolve()
    if not script.is_relative_to(lab / 'component-checks') or not script.is_file():
        raise ValueError('Publisher must be the independently staged candidate')
    old_export = Path(args.old_export).resolve()
    if not old_export.is_relative_to(lab / 'snapshots') or not (old_export / 'index.html').is_file():
        raise ValueError('Old export must be a real frozen export inside lab snapshots')
    config = json.loads((lab / 'current-qa.json').read_text())
    source = Path(config['workspace'])
    new_export = source / 'src/three-platform/web/out'
    assert (new_export / 'index.html').is_file(), 'Real frontend export is required'
    assert old_export != new_export, 'Need two distinct builds for the switch drill'

    root = lab / 'http-release-runs' / ('v2-' + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o755)
    site = 'zhihui.lab'
    cert, key = root / 'tls.crt', root / 'tls.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=' + site,
                    '-addext', 'subjectAltName=DNS:' + site, '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True)
    nginx_conf = root / 'nginx.conf'
    nginx_conf.write_text(f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 64; }}
http {{
    include /etc/nginx/mime.types;
    access_log {root}/access.log;
    server {{
        listen 127.0.0.1:{args.port} ssl;
        server_name {site};
        ssl_certificate {cert};
        ssl_certificate_key {key};
        root {root}/current;
        location / {{ try_files $uri $uri/ =404; }}
    }}
}}
''')
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise RuntimeError(f'Port {args.port} is already in use; leave the existing listener untouched')
    nginx = ['sudo', '-n', '/usr/sbin/nginx', '-p', str(root), '-c', str(nginx_conf)]
    result = {'host': socket.gethostname(), 'source_sha256': config['source_sha256'],
              'publisher': str(script), 'root': str(root), 'production_connected': False,
              'old_export': str(old_export), 'new_export': str(new_export),
              'started_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'checks': []}

    def pages_of(output: Path):
        return sorted({'/' if p.parent == output else '/' + p.parent.relative_to(output).as_posix() + '/'
                       for p in output.rglob('index.html') if '404' not in p.relative_to(output).parts})

    def publish(out_dir: Path, *, http: bool, link_mode: str = 'switch', name: str = 'run'):
        env = {**os.environ, 'OUT_DIR': str(out_dir), 'REL_ROOT': str(root / 'releases'),
               'LINK': str(root / 'current'), 'SITE': site, 'LOG': str(root / f'release-{name}.log'),
               'CURL_CA_BUNDLE': str(cert), 'KEEP_VERSIONS': '2',
               'HTTP_CHECK': '1' if http else '0', 'LINK_MODE': link_mode,
               'ZH_RELEASE_LOCK_FILE': str(root / 'release.lock'),
               'ZH_RELEASE_STATE_FILE': str(root / 'release-state.json'),
               'SITE_PORT': str(args.port),
               'PATH': config['node_bin'] + os.pathsep + os.environ.get('PATH', '')}
        for k in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            env[k] = ''
        env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost,' + site
        wrapper = source / 'ops/release-lock/zh-release-lock.sh'
        cmd = ['bash', str(wrapper), 'run', 'http-lab-' + root.name + '-' + name, 'web', '--',
               'bash', str(script)]
        env['PAGES'] = ' '.join(pages_of(out_dir))
        proc = subprocess.run(cmd, env=env, cwd=source, capture_output=True, timeout=900)
        return proc.returncode, proc

    def fetch(path: str):
        return subprocess.run(['curl', '-s', '-o', '/dev/null', '-m', '10', '-w', '%{http_code}',
                               '--cacert', str(cert), '--resolve', f'{site}:{args.port}:127.0.0.1',
                               f'https://{site}:{args.port}{path}'], capture_output=True, text=True).stdout.strip()

    def current_target():
        return (root / 'current').resolve(strict=True)

    def check(name: str, ok: bool, **extra):
        entry = {'name': name, 'passed': ok, **extra}
        result['checks'].append(entry)
        if not ok:
            raise AssertionError(f'drill check failed: {name} {extra}')

    try:
        subprocess.run([*nginx, '-t'], check=True, capture_output=True)
        subprocess.run(nginx, check=True, capture_output=True)

        # 1. seed the running version with the PREVIOUS real export
        rc, _ = publish(old_export, http=True, name='seed-old')
        check('old_build_published_all_pages_verified', rc == 0, exit=rc,
              old_build=current_target().name)
        old_dir = current_target()

        # 2. preview-install the NEW real export without switching
        rc, _ = publish(new_export, http=False, link_mode='none', name='preview-new')
        check('preview_install_no_switch', rc == 0 and current_target() == old_dir, exit=rc)
        new_bid = None
        for child in (root / 'releases').iterdir():
            if child.is_dir() and (child / 'BUILD_INFO.json').is_file() and child != old_dir:
                new_bid = child
        check('preview_release_complete', new_bid is not None
              and (new_bid / 'manifest.sha256').is_file())
        mtime_before = new_bid.stat().st_mtime_ns

        # 3. idempotent rerun completes the atomic switch
        rc, _ = publish(new_export, http=True, name='switch-new')
        check('idempotent_switch_all_pages_verified', rc == 0 and current_target() == new_bid, exit=rc)
        check('idempotent_reuse_dir_untouched', new_bid.stat().st_mtime_ns == mtime_before)

        # 4. old version retained and byte-identical after the switch
        verify = subprocess.run(
            ['bash', '-c', f'cd "{old_dir}" && awk -F\'  \' \'{{print $1"  "$3}}\' manifest.sha256 '
                           f'| sha256sum -c --quiet -'],
            capture_output=True)
        check('old_release_retained_and_verifiable', verify.returncode == 0 and old_dir.is_dir())

        # 5. K33 (fixed 2026-10-24): old chunks AND build-id manifests must remain
        #    reachable through the current root after the switch (link_old_static).
        old_chunk = next((old_dir / '_next' / 'static').rglob('*.js'))
        rel = old_chunk.relative_to(old_dir).as_posix()
        code_chunk = fetch('/' + rel)
        # 构建 ID 前缀的 manifest 是旧标签页最容易404的一类（K33 根因）
        bid_dirs = [d for d in (old_dir / '_next' / 'static').iterdir()
                    if d.is_dir() and d.name not in {'chunks', 'css', 'media', 'webpack'}]
        manifest_codes = {}
        for d in bid_dirs:
            for f in ('_buildManifest.js', '_ssgManifest.js'):
                pth = d / f
                if pth.is_file():
                    manifest_codes['/' + pth.relative_to(old_dir).as_posix()] = fetch(
                        '/' + pth.relative_to(old_dir).as_posix())
        all_ok = code_chunk == '200' and all(c == '200' for c in manifest_codes.values())
        check('old_chunk_via_current_root', all_ok, http_status=code_chunk,
              chunk=rel, manifests=manifest_codes,
              note='K33 fix: link_old_static links retained build-id dirs into the new release')

        # 6. failed candidate: valid structure, broken document -> rollback to verified new
        cand = root / 'candidate-c'
        shutil.copytree(new_export, cand)
        bids = [d.name for d in (cand / '_next').iterdir()
                if d.is_dir() and d.name != 'static' and len(d.name) >= 8]
        assert len(bids) == 1, bids
        (cand / '_next' / bids[0]).rename(cand / '_next' / 'qadrill00000001')
        (cand / 'dashboard' / 'index.html').write_text('<html><body>broken</body></html>')
        rc, _ = publish(cand, http=True, name='candidate-c')
        check('broken_candidate_rolled_back_to_verified_current',
              rc == 5 and current_target() == new_bid, exit=rc)
        check('current_pages_green_after_rollback', fetch('/dashboard/') == '200')

        # 7. corrupted CURRENT -> publish refused (rollback-target precheck)
        saved = (new_bid / 'index.html').read_bytes()
        (new_bid / 'index.html').write_bytes(saved + b'tampered\n')
        rc, _ = publish(old_export, http=False, name='refuse-corrupt')
        check('corrupted_current_refuses_publish', rc == 6, exit=rc)
        check('current_link_untouched_after_refusal', current_target() == new_bid)
        # restore for a clean evidence trail; republish old (idempotent) switch back
        (new_bid / 'index.html').write_bytes(saved)
        rc, _ = publish(old_export, http=True, name='after-restore')
        check('restored_current_accepts_publish_again', rc == 0 and current_target() == old_dir,
              exit=rc)

        result['status'] = 'passed'
        return 0
    finally:
        if (root / 'nginx.pid').exists():
            subprocess.run([*nginx, '-s', 'quit'], check=True, capture_output=True, timeout=30)
        result.setdefault('status', 'failed')
        result['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (root / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
