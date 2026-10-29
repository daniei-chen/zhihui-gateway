"""Extract publisher-verified archives without installing host software."""
import hashlib
import json
import subprocess
from pathlib import Path, PureWindowsPath

LAB = Path(__file__).resolve().parents[3] / 'zhihui-linux-lab'


def verify(name):
    plan = json.loads((LAB / 'download-plan.json').read_text(encoding='utf-8'))
    item = next(x for x in plan['items'] if x['name'] == name)
    source = LAB / 'downloads' / name
    h = hashlib.new(item['algorithm'])
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    if h.hexdigest() != item['digest']:
        raise RuntimeError('Archive verification failed: ' + name)
    return source


def extract(exe, source, folder):
    destination = LAB / 'tools' / folder
    marker = destination / '.archive-verified.json'
    if marker.exists():
        return destination
    listing = subprocess.run([str(exe), 'l', '-slt', str(source)], check=True,
                             capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
    paths = [line[7:] for line in listing.splitlines() if line.startswith('Path = ')]
    for value in paths:
        if value == str(source):
            continue
        path = PureWindowsPath(value)
        if path.is_absolute() or path.drive or '..' in path.parts:
            raise RuntimeError('Unsafe archive member')
    if 'Symbolic Link =' in listing or 'Hard Link =' in listing:
        raise RuntimeError('Unexpected archive link')
    destination.mkdir(parents=True, exist_ok=True)
    log = LAB / (folder + '-extract.log')
    with log.open('w', encoding='utf-8') as output:
        subprocess.run([str(exe), 'x', '-y', '-o' + str(destination), str(source)],
                       check=True, stdout=output, stderr=subprocess.STDOUT)
    marker.write_text(json.dumps({'source': source.name, 'members': len(paths) - 1}), encoding='utf-8')
    print('EXTRACTED', folder, flush=True)
    return destination


seven = extract(verify('7zr.exe'), verify('7z2605-x64.exe'), '7zip-26.05')
qemu_source = LAB / 'downloads' / 'qemu-w64-setup-20260811.exe'
if qemu_source.exists():
    qemu = extract(seven / '7z.exe', verify(qemu_source.name), 'qemu-20260811')
    executables = {name: [str(p) for p in qemu.rglob(name)]
                   for name in ('qemu-img.exe', 'qemu-system-x86_64.exe')}
    (LAB / 'portable-tools.json').write_text(json.dumps(executables, indent=2), encoding='utf-8')
    print(json.dumps(executables), flush=True)
else:
    print('QEMU download is still pending; 7-Zip extracted without installing it.', flush=True)
