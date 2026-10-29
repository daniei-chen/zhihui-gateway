"""K33 drill: cache policy + old-chunk retention across a real release switch.

在真实 Nginx 上：
  1) 发两代构建（A -> B），B 切换后旧版本目录仍在（KEEP_VERSIONS）
  2) 检查 Cache-Control：HTML 不缓存（no-cache/must-revalidate），hash 资源 immutable
  3) 检查旧标签页引用的 A 版 _next/static chunk 在切换后是否仍可取（200 而非 404）
  4) 检查不存在的资源是否 404（不因"全站禁缓存"掩盖）
  5) 检查 MIME 正确（js -> javascript, css -> css）

仅限一次性本地 VM。用高位端口，不干扰运行中的 fixture。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import uuid

PORT = 19445


def main() -> int:
    lab = Path('/home/gateway-lab/lab')
    if socket.gethostname() != 'zhihui-local-lab' or Path.home() != lab.parent:
        raise RuntimeError('Disposable local VM only')
    ap = argparse.ArgumentParser()
    ap.add_argument('--script', required=True)
    ap.add_argument('--old-export', required=True)
    args = ap.parse_args()
    script = Path(args.script).resolve()
    old_export = Path(args.old_export).resolve()
    cfg = json.loads((lab / 'current-qa.json').read_text())
    source = Path(cfg['workspace'])
    new_export = source / 'src/three-platform/web/out'
    assert script.is_file() and (old_export / 'index.html').is_file() and (new_export / 'index.html').is_file()

    root = lab / 'http-release-runs' / ('k33-' + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    site = 'zhihui.lab'
    cert, key = root / 'tls.crt', root / 'tls.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=' + site,
                    '-addext', 'subjectAltName=DNS:' + site, '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True)
    # 与生产 cache-policy 一致的 Nginx 片段
    (root / 'nginx.conf').write_text(f'''user gateway-lab;
worker_processes 1;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 256; }}
http {{
    include /etc/nginx/mime.types;
    access_log {root}/access.log;
    server {{
        listen 127.0.0.1:{PORT} ssl;
        server_name {site};
        ssl_certificate {cert};
        ssl_certificate_key {key};
        root {root}/current;
        # 与 ops/nginx/cache-policy.design.md 对齐的记忆化策略
        location /_next/static/ {{
            add_header Cache-Control "public, max-age=31536000, immutable";
            try_files $uri =404;
        }}
        location / {{
            add_header Cache-Control "no-cache, must-revalidate";
            try_files $uri $uri/ =404;
        }}
    }}
}}
''')
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', PORT)) == 0:
            raise RuntimeError(f'Port {PORT} busy')
    nginx = ['sudo', '-n', '/usr/sbin/nginx', '-p', str(root), '-c', str(root / 'nginx.conf')]
    result = {'drill': 'K33-cache-and-old-chunks', 'host': socket.gethostname(),
              'source_sha256': cfg['source_sha256'], 'root': str(root),
              'production_connected': False, 'port': PORT, 'checks': []}

    def pages_of(out):
        return sorted({'/' if p.parent == out else '/' + p.parent.relative_to(out).as_posix() + '/'
                       for p in out.rglob('index.html') if '404' not in p.relative_to(out).parts})

    def publish(out_dir, name):
        env = {**os.environ, 'OUT_DIR': str(out_dir), 'REL_ROOT': str(root / 'releases'),
               'LINK': str(root / 'current'), 'SITE': site, 'SITE_PORT': str(PORT),
               'LOG': str(root / f'release-{name}.log'), 'CURL_CA_BUNDLE': str(cert),
               'KEEP_VERSIONS': '3', 'HTTP_CHECK': '1', 'LINK_MODE': 'switch',
               'ZH_RELEASE_LOCK_FILE': str(root / 'release.lock'),
               'ZH_RELEASE_STATE_FILE': str(root / 'release-state.json'),
               'PAGES': ' '.join(pages_of(out_dir)),
               'PATH': cfg['node_bin'] + os.pathsep + os.environ.get('PATH', '')}
        for k in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            env[k] = ''
        env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost,' + site
        wrapper = source / 'ops/release-lock/zh-release-lock.sh'
        proc = subprocess.run(['bash', str(wrapper), 'run', 'k33-' + root.name + '-' + name, 'web',
                               '--', 'bash', str(script)], env=env, cwd=source,
                              capture_output=True, timeout=900)
        return proc.returncode, proc

    def curl(path, want_headers=False):
        cmd = ['curl', '-s', '-m', '10', '-o', '/dev/null', '-w', '%{http_code}|%{content_type}',
               '--cacert', str(cert), '--resolve', f'{site}:{PORT}:127.0.0.1']
        if want_headers:
            cmd += ['-D', '-']
        cmd.append(f'https://{site}:{PORT}{path}')
        r = subprocess.run(cmd, capture_output=True, text=True)
        if want_headers:
            hdrs = {}
            for line in r.stdout.splitlines():
                if ':' in line and not line.startswith('HTTP/'):
                    k, _, v = line.partition(':')
                    hdrs[k.strip().lower()] = v.strip()
            last = r.stdout.strip().splitlines()[-1]
            code, _, ctype = last.partition('|')
            return code.strip(), ctype.strip(), hdrs
        code, _, ctype = r.stdout.partition('|')
        return code.strip(), ctype.strip(), {}

    def check(name, ok, **extra):
        e = {'name': name, 'passed': bool(ok), **extra}
        result['checks'].append(e)
        return ok

    try:
        subprocess.run([*nginx, '-t'], check=True, capture_output=True)
        subprocess.run(nginx, check=True, capture_output=True)
        time.sleep(0.6)
        rc, proc = publish(old_export, 'A')
        if rc != 0:
            result['error'] = 'publish A failed: ' + proc.stdout.decode('utf-8', 'replace')[-800:]
            raise SystemExit(2)
        # 记录 A 版引用的 chunk（模拟旧标签页持有的 URL）
        a_chunks = []
        for f in sorted((old_export / '_next/static').rglob('*')):
            if f.is_file() and f.suffix in ('.js', '.css'):
                a_chunks.append('/' + f.relative_to(old_export).as_posix())
        a_sample = a_chunks[:5]
        code_a, _, _ = curl('/')
        result['A'] = {'index_code': code_a, 'chunk_sample': a_sample, 'chunk_total': len(a_chunks)}

        rc, proc = publish(new_export, 'B')
        if rc != 0:
            result['error'] = 'publish B failed: ' + proc.stdout.decode('utf-8', 'replace')[-800:]
            raise SystemExit(2)
        code_b, _, html_hdrs = curl('/', want_headers=True)
        result['B'] = {'index_code': code_b, 'cache_control': html_hdrs.get('cache-control')}
        check('html_served_after_switch', code_b == '200', code=code_b)
        cc = (html_hdrs.get('cache-control') or '').lower()
        check('html_not_long_cached', ('no-cache' in cc or 'must-revalidate' in cc), cache_control=cc)

        # 旧 chunk 在切换后是否仍可取（K33 关键）
        old_chunk_results = []
        for path in a_sample:
            code, ctype, hdrs = curl(path, want_headers=True)
            old_chunk_results.append({'path': path, 'code': code, 'ctype': ctype,
                                      'cache_control': hdrs.get('cache-control')})
        result['old_chunks'] = old_chunk_results
        still_ok = [c for c in old_chunk_results if c['code'] == '200']
        check('old_chunks_reachable_after_switch', len(still_ok) == len(a_sample),
              reachable=f'{len(still_ok)}/{len(a_sample)}', detail=old_chunk_results)

        # hash 资源 immutable
        if old_chunk_results:
            cc0 = (old_chunk_results[0]['cache_control'] or '').lower()
            check('hashed_asset_immutable', 'immutable' in cc0 and 'max-age=31536000' in cc0,
                  cache_control=cc0)

        # 不存在资源必须 404（不能被 catch-all 兜成 200）
        miss_code, _, _ = curl('/_next/static/chunks/definitely-missing-chunk.js')
        check('missing_asset_404', miss_code == '404', code=miss_code)

        # MIME
        js = next((c for c in old_chunk_results if c['path'].endswith('.js')), None)
        css = next((c for c in old_chunk_results if c['path'].endswith('.css')), None)
        if js:
            check('js_mime', 'javascript' in (js['ctype'] or ''), ctype=js['ctype'])
        if css:
            check('css_mime', 'css' in (css['ctype'] or ''), ctype=css['ctype'])

        # 保留目录数（KEEP_VERSIONS=3）
        rel = root / 'releases'
        kept = sorted(p.name for p in rel.iterdir() if p.is_dir() and not p.name.startswith('.'))
        result['kept_releases'] = kept

        result['status'] = 'passed' if all(c['passed'] for c in result['checks']) else 'failed'
        return 0 if result['status'] == 'passed' else 1
    finally:
        try:
            subprocess.run([*nginx, '-s', 'stop'], capture_output=True, timeout=20)
        except Exception:
            pass
        (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(json.dumps({c['name']: c['passed'] for c in result['checks']}, ensure_ascii=False))
        print('RESULT ' + str(root / 'result.json'))


if __name__ == '__main__':
    raise SystemExit(main())
