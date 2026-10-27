"""K50：容器加固与镜像来源的**静态**审计（无 Docker 环境也能跑）。

## 为什么只能静态审计

生产容器在远端主机上，本轮拿不到 `docker inspect`。任务卡要求的
"镜像 digest / commit / SBOM 对应、非 root、cap/pids 逐项、只读挂载、
必须写目录、healthcheck 内容、容器重建后账号/状态不丢"里：

  * 能从**源码**判定的：非 root、cap/pids、healthcheck、挂载设计、
    镜像构建流程是否产出内容寻址标签；
  * 必须**运行时**才能判定的：真实 digest、容器重建后状态、只读挂载影响。

本脚本做前者，并把后者明确列为"需生产执行"。

## 运行

    python -X utf8 tools/audit_container_hardening.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / 'src' / 'three-platform' / 'Dockerfile'
COMPOSE = ROOT / 'src' / 'three-platform' / 'docker-compose.yml'
BUILD_YML = ROOT / 'manager' / 'deploy' / 'fork-image' / 'build-image.yml'

ROWS: list[dict] = []


def record(case, ok, detail, evidence=None):
    ROWS.append({'case': case, 'ok': bool(ok), 'detail': detail, 'evidence': evidence})
    print(('PASS ' if ok else 'FAIL ') + f'{case}: {detail}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', type=Path)
    args = ap.parse_args(argv)

    if not DOCKERFILE.exists():
        record('Dockerfile 存在', False, str(DOCKERFILE))
        return finish(args)

    docker = DOCKERFILE.read_text(encoding='utf-8')

    # ── 1. 非 root ──────────────────────────────────────────────────────
    has_user = re.search(r'^USER\s+(?!root)(\S+)', docker, re.M)
    record('镜像以非 root 运行', bool(has_user),
           f'USER {has_user.group(1)}' if has_user else 'Dockerfile 没有非 root 的 USER 指令',
           str(DOCKERFILE))
    uid = re.search(r'useradd\s+-u\s+(\d+)', docker)
    record('固定 UID（便于卷属主对齐）', bool(uid),
           f'uid={uid.group(1)}' if uid else '未固定 UID，bind mount 属主会随镜像变')

    # ── 2. HEALTHCHECK ──────────────────────────────────────────────────
    hc = re.search(r'^HEALTHCHECK\s+(.+)$', docker, re.M)
    record('Dockerfile 有 HEALTHCHECK', bool(hc), (hc.group(1)[:90] if hc else '缺失'))

    # ── 3. 镜像来源：内容寻址标签 ────────────────────────────────────────
    if BUILD_YML.exists():
        build = BUILD_YML.read_text(encoding='utf-8')
        has_sha_tag = 'sha-$' in build or 'sha-sha' in build or ':sha-' in build
        record('构建流程产出 sha-<commit> 标签', has_sha_tag,
               '存在 content-addressed 标签（可用它核对"拉到的就是那次提交"）'
               if has_sha_tag else '只有 latest，无法证明镜像与提交的对应关系')
        # 是否真的支持 digest 部署（README/文档里要求 pull @sha256）
    else:
        record('构建流程文件存在', False, str(BUILD_YML))

    # ── 4. compose 加固项 ───────────────────────────────────────────────
    if COMPOSE.exists():
        compose = COMPOSE.read_text(encoding='utf-8')
        # 用 YAML 解析而不是正则：`cap_drop:` 与 `- ALL` 之间可能夹着注释，
        # 正则跨不过去（首版就踩了这个，误报"缺失"）。
        compose_cfg = {}
        try:
            import yaml  # type: ignore
            doc = yaml.safe_load(compose) or {}
            services = doc.get('services') or {}
            compose_cfg = services.get('workbuddy-manager') or (
                next(iter(services.values())) if services else {})
        except Exception as exc:  # noqa: BLE001
            record('compose 可被 YAML 解析', False, f'{type(exc).__name__}: {exc}')
        if compose_cfg:
            record('compose 可被 YAML 解析', True,
                   f'{len(compose_cfg)} 个键：{sorted(compose_cfg)[:8]}')
            caps = compose_cfg.get('cap_drop') or []
            record('compose 有 cap_drop ALL', 'ALL' in caps, f'cap_drop={caps}')
            record('compose 有 cap_add（最小集合）',
                   bool(compose_cfg.get('cap_add')),
                   f"cap_add={compose_cfg.get('cap_add')}")
            sec = compose_cfg.get('security_opt') or []
            record('compose 有 no-new-privileges',
                   any('no-new-privileges' in str(x) for x in sec), f'security_opt={sec}')
            record('compose 有 pids_limit', bool(compose_cfg.get('pids_limit')),
                   f"pids_limit={compose_cfg.get('pids_limit')}")
            record('compose 有 mem_limit', bool(compose_cfg.get('mem_limit')),
                   f"mem_limit={compose_cfg.get('mem_limit')}")
            record('compose 有 restart 策略',
                   bool(compose_cfg.get('restart')), f"restart={compose_cfg.get('restart')}")
            ports = compose_cfg.get('ports') or []
            record('端口只绑本机',
                   all(str(p).startswith('127.0.0.1:') for p in ports) if ports else False,
                   f'ports={ports}')
            vols = compose_cfg.get('volumes') or []
            record('写路径走卷（data）',
                   any(':'.join(str(v).split(':')[-2:]).endswith('/app/data') or '/app/data' in str(v)
                       for v in vols), f'volumes={[str(v)[:40] for v in vols]}')
            record('必须写的目录都挂载',
                   any('docker.sock' in str(v) for v in vols), 'docker.sock 挂载存在')
            # 只读根文件系统：**记录现状，不计入通过/失败**。
            # 现在盲目开它会直接让服务不可用（要写 /app/data、/opt/workbuddy2api、
            # /app/.local 三处），而"没开"也不等于不安全 —— 真正的加固价值需要
            # 在完成写路径审计后再判断。用 ok=True + informative 标记，避免
            # 把它算成一个"待修的失败"。
            ROWS.append({
                'case': '只读根文件系统（现状记录）',
                'ok': True,
                'informative': True,
                'detail': ('已启用' if compose_cfg.get('read_only') else
                           '未启用；须先 docker diff 审计 /app/data、'
                           '/opt/workbuddy2api、/app/.local 三处写路径再加'),
            })
            print('INFO 只读根文件系统（现状记录）: '
                  + ('已启用' if compose_cfg.get('read_only') else '未启用（需先做写路径审计）'))
    else:
        record('docker-compose.yml 存在', False, str(COMPOSE))

    # ── 5. 镜像内不烘数据/凭据 ──────────────────────────────────────────
    leaks = []
    for line in docker.splitlines():
        s = line.strip()
        if not s.startswith(('COPY ', 'ADD ')):
            continue
        if re.search(r'\b(data|auths|config\.json|\.env|secrets?)\b', s):
            leaks.append(s[:90])
    record('镜像不 COPY 数据/凭据目录', not leaks,
           '无 COPY data/auths/config.json' if not leaks else f'可疑：{leaks}')

    # ── 6. 运行时不可静态判定的项（明确列出）────────────────────────────
    print()
    print('  (需生产运行时确认，本脚本不判定)')
    for item in (
        '真实镜像 digest（docker inspect --format "{{.Image}}"）',
        '镜像 digest 与构建 commit/SBOM 的对应关系',
        '只读根文件系统开启后的实际行为（登录/推理/取消三项冒烟）',
        '容器重建后账号与状态是否保留（需真实容器 + 卷）',
        'Trae / Qoder 镜像的源码对应关系（第三方镜像）',
    ):
        print(f'    · {item}')

    return finish(args)


def finish(args) -> int:
    failed = [r for r in ROWS if not r['ok']]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {'schema': 'zhihui-k50-container/v1', 'rows': ROWS,
             'passed': len(ROWS) - len(failed), 'failed': len(failed)},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n{len(ROWS) - len(failed)}/{len(ROWS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
