"""Exercise the candidate web publisher against real Nginx and verified TLS.

Restricted to the disposable local VM. No production host, paths or credentials.
Keeps the test root and logs as evidence; stops only its own Nginx instance.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import uuid


def main() -> int:
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('This check may run only in the disposable local VM')
    parser = argparse.ArgumentParser()
    parser.add_argument('--script', required=True)
    args = parser.parse_args()
    script = Path(args.script).resolve()
    if not script.is_relative_to(lab / 'component-checks') or not script.is_file():
        raise ValueError('Publisher must be the independently staged candidate')
    config = json.loads((lab / 'current-qa.json').read_text())
    source = Path(config['workspace'])
    output = source / 'src/three-platform/web/out'
    assert (output / 'index.html').is_file(), 'Real frontend export is required'
    root = lab / 'http-release-runs' / uuid.uuid4().hex[:12]
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
        listen 127.0.0.1:443 ssl;
        server_name {site};
        ssl_certificate {cert};
        ssl_certificate_key {key};
        root {root}/current;
        location / {{ try_files $uri $uri/ =404; }}
    }}
}}
''')
    # Port 443 is private to this VM and must be free. Never stop another listener.
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', 443)) == 0:
            raise RuntimeError('Port 443 is already in use; leave the existing listener untouched')
    nginx = ['sudo', '-n', '/usr/sbin/nginx', '-p', str(root), '-c', str(nginx_conf)]
    result = {'host': socket.gethostname(), 'source_sha256': config['source_sha256'],
              'publisher': str(script), 'root': str(root), 'production_connected': False,
              'started_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'checks': []}
    env = {**os.environ, 'OUT_DIR': str(output), 'REL_ROOT': str(root / 'releases'),
           'LINK': str(root / 'current'), 'SITE': site, 'LOG': str(root / 'release.log'),
           'CURL_CA_BUNDLE': str(cert), 'KEEP_VERSIONS': '2', 'HTTP_CHECK': '1',
           'ZH_RELEASE_LOCK_FILE': str(root / 'release.lock'),
           'ZH_RELEASE_STATE_FILE': str(root / 'release-state.json'),
           'PATH': config['node_bin'] + os.pathsep + os.environ.get('PATH', '')}
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        env[name] = ''
    env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost,' + site
    pages = sorted({'/' if path.parent == output else '/' + path.parent.relative_to(output).as_posix() + '/'
                    for path in output.rglob('index.html')
                    if '404' not in path.relative_to(output).parts})
    env['PAGES'] = ' '.join(pages)
    wrapper = source / 'ops/release-lock/zh-release-lock.sh'
    try:
        subprocess.run([*nginx, '-t'], check=True, capture_output=True)
        subprocess.run(nginx, check=True, capture_output=True)
        with (root / 'driver.log').open('w') as log:
            command = ['bash', str(wrapper), 'run', 'http-lab-' + root.name, 'web',
                       '--', 'bash', str(script)]
            publication = subprocess.run(command, env=env, cwd=source,
                                         stdout=log, stderr=subprocess.STDOUT, timeout=900)
        result['publisher_exit'] = publication.returncode
        assert publication.returncode == 0, 'Publisher failed; inspect release.log'
        target = (root / 'current').resolve(strict=True)
        assert target.is_relative_to(root / 'releases')
        info = json.loads((target / 'BUILD_INFO.json').read_text())
        assert info['buildId'] == target.name
        result['build_id'] = target.name
        result['checks'].append({'name': 'all_real_exported_pages_and_sampled_js_css',
                                 'passed': True, 'pages': len(pages), 'tls_verified': True})
        # An existing release must be verified without replacing its directory.
        before = target.stat().st_mtime_ns
        again = subprocess.run(command, env=env, cwd=source, capture_output=True, timeout=900)
        assert again.returncode == 0
        assert target.stat().st_mtime_ns == before
        result['checks'].append({'name': 'identical_release_directory_not_replaced', 'passed': True})
        # A 200 response with an invalid document must fail and restore the old link.
        bad = root / 'bad-output'
        for folder in ('dashboard', 'login', 'keys', '_next/badbuild12345678', '_next/static/chunks'):
            (bad / folder).mkdir(parents=True, exist_ok=True)
        for page in ('index.html', 'dashboard/index.html', 'login/index.html', 'keys/index.html'):
            (bad / page).write_text('<html><body>intentionally invalid QA candidate</body></html>')
        (bad / '_next/static/chunks/qa.js').write_text('console.log("qa");')
        invalid_env = {**env, 'OUT_DIR': str(bad), 'PAGES': '/ /dashboard/ /login/ /keys/'}
        rejected = subprocess.run(command, env=invalid_env, cwd=source,
                                  capture_output=True, timeout=300)
        assert rejected.returncode == 5, rejected.stderr.decode(errors='replace')
        assert (root / 'current').resolve(strict=True) == target
        result['checks'].append({'name': 'http_200_invalid_document_rejected_and_rolled_back', 'passed': True})
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
