"""Prepare a disposable local Ubuntu VM, its own SSH key and NoCloud seed."""
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

LAB = Path(__file__).resolve().parents[3] / 'zhihui-linux-lab'
verified = json.loads((LAB / 'verified-downloads.json').read_text(encoding='utf-8'))
image = next(x for x in verified if x['name'].endswith('.img'))
source = Path(image['path'])
with source.open('rb') as stream:
    h = hashlib.file_digest(stream, 'sha256').hexdigest()
if h != image['digest']:
    raise RuntimeError('Base image verification failed')
tools = json.loads((LAB / 'portable-tools.json').read_text(encoding='utf-8'))
img = tools['qemu-img.exe'][0]
key = LAB / 'lab_ed25519'
if not key.exists():
    subprocess.run(['ssh-keygen.exe', '-q', '-t', 'ed25519', '-N', '',
                    '-C', 'zhihui-local-linux-lab', '-f', str(key)], check=True)
public_key = key.with_suffix('.pub').read_text(encoding='utf-8').strip()
seed = LAB / 'cloud-init'
seed.mkdir(exist_ok=True)
meta = seed / 'meta-data'
if not meta.exists():
    meta.write_text(f'instance-id: zhihui-local-{uuid.uuid4().hex}\n'
                    'local-hostname: zhihui-local-lab\n', encoding='utf-8')
(seed / 'user-data').write_text('''#cloud-config
users:
  - name: gateway-lab
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - ''' + public_key + '''
ssh_pwauth: false
disable_root: true
package_update: false
package_upgrade: false
runcmd:
  - [sh, -c, 'echo LOCAL_LINUX_LAB_READY > /var/tmp/zhihui-lab-ready']
''', encoding='utf-8')
disk = LAB / 'ubuntu-lab.qcow2'
if not disk.exists():
    info = json.loads(subprocess.check_output([img, 'info', '--output=json', str(source)], text=True))
    subprocess.run([img, 'create', '-f', 'qcow2', '-F', info['format'],
                    '-b', str(source), str(disk), '20G'], check=True)
arguments = ['-accel', 'tcg,thread=multi', '-cpu', 'max', '-smp', '2', '-m', '2048',
             '-display', 'none', '-drive', f'file={disk},if=virtio,format=qcow2',
             '-serial', 'file:' + str(LAB / 'boot.log'),
             '-smbios', 'type=1,serial=ds=nocloud-net;s=http://10.0.2.2:18181/',
             '-nic', 'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:22224-:22,'
                     'hostfwd=tcp:127.0.0.1:18086-:80,hostfwd=tcp:127.0.0.1:18443-:443',
             '-qmp', 'tcp:127.0.0.1:18446,server=on,wait=off']
config = {'purpose': 'local isolated verification; no production data', 'ram_mib': 2048,
          'vcpus': 2, 'disk': str(disk), 'qemu': tools['qemu-system-x86_64.exe'][0],
          'arguments': arguments, 'ssh_host': '127.0.0.1', 'ssh_port': 22224,
          'ssh_user': 'gateway-lab', 'ssh_key_path': str(key),
          'seed_directory': str(seed), 'production_connected': False}
(LAB / 'lab-config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
print(json.dumps({'status': 'prepared', 'ram_mib': 2048, 'vcpus': 2,
                  'ports': [18181, 22224, 18086, 18443, 18446]}), flush=True)
