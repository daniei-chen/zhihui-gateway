"""Download verified, portable prerequisites for the LOCAL Linux test VM.

No installer is executed, no host service is installed, and production is never
contacted. QEMU and Ubuntu digests are pinned to the publisher records inspected
on 2026-10-24. 7-Zip assets use the publisher's GitHub release asset digest.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[2]
LAB = REPO.parent / 'zhihui-linux-lab'
DOWNLOADS = LAB / 'downloads'
HEADERS = {'User-Agent': 'zhihui-local-verification/1.0'}


def fetch_json(url):
    with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
        return json.load(response)


def digest_file(path, algorithm):
    h = hashlib.new(algorithm)
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download(item):
    destination = DOWNLOADS / item['name']
    assert destination.resolve().parent == DOWNLOADS.resolve()
    if destination.exists():
        if digest_file(destination, item['algorithm']) != item['digest']:
            raise RuntimeError(f"Existing artifact failed verification: {item['name']}")
        return {**item, 'path': str(destination), 'bytes': destination.stat().st_size,
                'status': 'verified-existing'}
    partial = destination.with_suffix(destination.suffix + '.part')
    for attempt in range(3):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            headers = dict(HEADERS)
            if offset:
                headers['Range'] = f'bytes={offset}-'
            with urlopen(Request(item['url'], headers=headers), timeout=30) as response:
                resumed = offset > 0 and response.status == 206
                total = int(response.headers.get('Content-Length', '0')) + (offset if resumed else 0)
                if total > 4 * 1024**3:
                    raise RuntimeError('Artifact exceeds 4 GiB download limit')
                written = offset if resumed else 0
                milestone = written // (64 * 1024**2)
                with partial.open('ab' if resumed else 'wb') as stream:
                    for chunk in iter(lambda: response.read(1024 * 1024), b''):
                        written += len(chunk)
                        if written > 4 * 1024**3:
                            raise RuntimeError('Artifact exceeds download limit')
                        stream.write(chunk)
                        if written // (64 * 1024**2) > milestone:
                            milestone = written // (64 * 1024**2)
                            print(f"{item['name']}: {written // 1024**2} MiB", flush=True)
            if digest_file(partial, item['algorithm']) != item['digest']:
                raise RuntimeError(f"Publisher checksum mismatch: {item['name']}")
            os.replace(partial, destination)
            print(f"VERIFIED {item['name']}", flush=True)
            return {**item, 'path': str(destination), 'bytes': destination.stat().st_size,
                    'status': 'verified'}
        except Exception as exc:
            if 'checksum' in str(exc).lower() or attempt == 2:
                raise
            print(f"Retrying {item['name']}: {type(exc).__name__}", flush=True)
            time.sleep(2)


def main():
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    release = fetch_json('https://api.github.com/repos/ip7z/7zip/releases/tags/26.05')
    assets = {asset['name']: asset for asset in release['assets']}
    items = [
        {'name': 'qemu-w64-setup-20260811.exe',
         'url': 'https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe',
         'algorithm': 'sha512',
         'digest': '5bcf9eed634e8575a37b74f445af41a2fe4106da512d0c30c368301d4c105037fdfab40a5287367a28a957624cddebbc8c07e16c88ab6634f554cdf3d16bf543',
         'publisher_record': 'https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.sha512'},
        {'name': 'ubuntu-noble-amd64-04e8f2461a16.img',
         'url': 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img',
         'algorithm': 'sha256',
         'digest': '04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94',
         'publisher_record': 'https://cloud-images.ubuntu.com/noble/current/SHA256SUMS'},
    ]
    for name in ('7z2605-x64.exe', '7z2605-x64.msi', '7zr.exe'):
        asset = assets[name]
        checksum = asset.get('digest', '')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', checksum):
            raise RuntimeError(f'Publisher digest missing for {name}; refusing unverified binary')
        items.append({'name': name, 'url': asset['browser_download_url'], 'algorithm': 'sha256',
                      'digest': checksum.split(':', 1)[1],
                      'publisher_record': release['html_url']})
    (LAB / 'download-plan.json').write_text(json.dumps(
        {'pid': os.getpid(), 'purpose': 'local isolated verification only', 'items': items},
        ensure_ascii=False, indent=2), encoding='utf-8')
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(download, items):
            results.append(result)
    (LAB / 'verified-downloads.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'verified', 'artifacts': len(results), 'root': str(LAB)}), flush=True)


if __name__ == '__main__':
    main()
