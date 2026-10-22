#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 网关健康巡检 + 告警（systemd timer 每 5 分钟跑一次）。

规则全部来自 2026-10-05/06 两次真实故障的特征：
  R1 网关宕机      workbuddy-web 非 active / 容器非 healthy / /v1/models 探活失败   → CRITICAL
  R2 上游僵死      容器日志 10 分钟内 "attempt timeout" >= 3（上游不吐响应头被迫换号） → WARN
  R3 网关拒绝/池干涸 管理器日志 10 分钟内 gateway_capacity_exceeded / no healthy account / 503 >= 3 → WARN
  R4 上游限流      容器日志 10 分钟内 soft_rate（模型繁忙 429）>= 5                  → INFO

通知渠道（/etc/wb-gateway-alert.json 控制开关）：
  wechat: POST http://127.0.0.1:3080/api/dsh-im/delivery/messages {botId,targetId,text}
          —— targetId 需先在 DSH 面板「设置 → IM机器人 → 新建目标」创建
  email : smtplib + QQ SMTP SSL(465)，需 QQ 邮箱授权码（不是 QQ 密码）

同一规则 60 分钟冷却；告警后恢复会补发一条「已恢复」。所有动作写 /var/log/wb-gateway-alert.log。
"""
from __future__ import annotations

import json
import html
import socket
import sys
import smtplib
import subprocess
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.utils import formataddr
from pathlib import Path

CONFIG = Path('/etc/wb-gateway-alert.json')
STATE = Path('/var/lib/wb-gateway-alert/state.json')
LOG = Path('/var/log/wb-gateway-alert.log')
FALLBACK = Path('/var/log/wb-gateway-alerts.fired')   # 无渠道时告警正文落盘处
WINDOW_MIN = 10
COOLDOWN_MIN = 60
# 每条规则各自的冷却（分钟）；缺省 COOLDOWN_MIN。R6=异地备份未配，属长期状态，24h 提一次。
RULE_COOLDOWN_MIN = {'R6': 1440, 'R5': 360}


def log(msg: str) -> None:
    line = f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
    print(line)
    try:
        with LOG.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def run(cmd: list[str], timeout: int = 60) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as exc:  # noqa: BLE001
        return f'__error__ {exc}'


def sh(cmd: str, timeout: int = 60) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as exc:  # noqa: BLE001
        return f'__error__ {exc}'


def client_errors(minutes: int = 12) -> dict:
    """直接读 manager.db 的 request_logs：这才是客户端真实收到的错误。
    只读打开（WAL 允许并发读），任何异常都不影响巡检本身。"""
    import sqlite3
    db = '/opt/workbuddy-manager/data/manager.db'
    out = {'total': 0, 'concurrency': 0, 'quota': 0, 'capacity': 0,
           'no_healthy': 0, 'encode': 0, 'upstream_slow': 0, 'sample': ''}
    try:
        uri = f'file:{db}?mode=ro&immutable=0'
        con = sqlite3.connect(uri, uri=True, timeout=5)
        rows = con.execute(
            "select coalesce(error,''), count(*) from request_logs "
            "where ts > strftime('%s','now') - ? * 60 and coalesce(error,'') <> '' "
            "group by 1", (minutes,)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return out
    for text, n in rows:
        out['total'] += n
        if '密钥并发' in text or 'concurrency' in text:
            out['concurrency'] += n
        elif '配额' in text or 'quota' in text:
            out['quota'] += n
        elif '容量已满' in text or 'capacity' in text:
            out['capacity'] += n
        elif 'no_healthy' in text or 'no healthy' in text:
            out['no_healthy'] += n
        elif 'utf-8' in text or 'surrogate' in text:
            out['encode'] += n
        elif 'timeout' in text or 'timed out' in text:
            out['upstream_slow'] += n
        if not out['sample']:
            out['sample'] = text[:60]
    return out


def backup_status() -> dict:
    """每日备份的 offsite 标志与新鲜度（审计遗留：异地备份未配置 / 备份可能静默失败）。"""
    import time
    out = {'age_h': None, 'offsite': None, 'error': ''}
    latest = Path('/root/backups/workbuddy-daily/latest.json')
    try:
        meta = json.loads(latest.read_text(encoding='utf-8'))
        out['age_h'] = (time.time() - latest.stat().st_mtime) / 3600
        out['offsite'] = bool(meta.get('offsite'))
        out['error'] = str(meta.get('offsite_error', ''))
    except Exception:  # noqa: BLE001
        pass
    return out


def _gateway_key() -> str:
    try:
        import re
        text = Path('/home/agentuser/.dsh/.credentials.yaml').read_text(encoding='utf-8')
        found = re.search(r'wbk_[A-Za-z0-9_-]{20,}', text)
        return found.group(0) if found else ''
    except Exception:  # noqa: BLE001
        return ''


# ---------------------------------------------------------------- 检测规则
def collect() -> dict:
    # docker logs 的容器 stderr（Go 的 log.Printf：attempt timeout / soft_rate / 冷却 等）
    # 只取 .stdout 会整片丢掉——实测 12 分钟窗口 stdout 18 行 / 合并后 108 行。
    container_logs = sh(f'docker logs --since {WINDOW_MIN + 1}m workbuddy2api 2>&1', 90)
    manager_journal = sh(
        'journalctl -u workbuddy-web --since "-12 min" --no-pager -q 2>/dev/null')
    service = sh('systemctl is-active workbuddy-web').strip()
    try:
        _cfg = json.loads(Path('/opt/workbuddy2api/config.json').read_text(encoding='utf-8'))
        _wd = _cfg.get('upstream', {}).get('attempt_timeout_seconds')
        watchdog = f'{_wd}s' if _wd else '配置值'
    except Exception:  # noqa: BLE001
        watchdog = '配置值'
    health = sh("docker inspect workbuddy2api --format '{{.State.Health.Status}}'").strip()
    cmd = ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '-m', '10']
    key = _gateway_key()
    if key:
        cmd += ['-H', f'Authorization: Bearer {key}']
    cmd.append('http://127.0.0.1:7864/v1/models')
    probe = run(cmd, 15).strip()
    return {
        'timeout_n': container_logs.count('attempt timeout'),
        'soft_rate_n': container_logs.count('soft_rate'),
        # WorkBuddy 风控拒绝（code=11-128「请求被渠道校验拦截」）。
        # 这是上游识别第三方客户端的信号：出现频次突增说明它加了新规则。
        'risk_1128_n': container_logs.count('11-128'),
        'cap_n': manager_journal.count('gateway_capacity_exceeded'),
        'pool_n': manager_journal.count('no healthy account') + manager_journal.count('no_healthy_account'),
        's503_n': manager_journal.count('" 503 '),
        'service': service,
        'health': health,
        'probe': probe,
        'watchdog': watchdog,
        'client': client_errors(WINDOW_MIN + 2),
        'backup': backup_status(),
        'res': collect_resources(),
    }


def collect_resources() -> dict:
    """宿主机资源异常：swap 压力 / 僵尸进程 / 单进程内存失控。

    为什么需要：2026-10-22 发现一个 grep 因正则灾难性回溯在机器上卡了
    3 天 14 小时，吃掉 1GB swap，而现有告警只看「服务活不活」，完全没察觉。
    """
    out = {'swap_pct': None, 'swap_used_mb': None, 'zombies': 0, 'fat': []}
    try:
        info = {}
        for line in open('/proc/meminfo', encoding='utf-8'):
            k, _, v = line.partition(':')
            info[k] = v.strip()
        total = int(info.get('SwapTotal', '0 kB').split()[0])
        free = int(info.get('SwapFree', '0 kB').split()[0])
        if total > 0:
            out['swap_used_mb'] = (total - free) // 1024
            out['swap_pct'] = round(100.0 * (total - free) / total, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        import glob
        for f in glob.glob('/proc/[0-9]*/status'):
            try:
                state = rss = None
                comm = ''
                for line in open(f, encoding='utf-8', errors='replace'):
                    if line.startswith('State:'):
                        state = line.split()[1]
                    elif line.startswith('VmRSS:'):
                        rss = int(line.split()[1])
                pid = f.split('/')[2]
                try:
                    comm = open('/proc/' + pid + '/comm', encoding='utf-8').read().strip()
                except OSError:
                    comm = '?'
                if state == 'Z':
                    out['zombies'] += 1
                # 只报「非核心进程」的超大内存：node/python/dockerd 本来就大
                if rss and rss > 1024 * 1024 and comm not in ('node', 'python', 'python3', 'dockerd', 'java'):
                    out['fat'].append(f'{comm}({pid}) {rss // 1024}MB')
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def evaluate(m: dict) -> dict:
    """返回 {rule_id: (level, text)}，只含触发的规则。"""
    fired = {}
    if m['service'] != 'active' or m['health'] != 'healthy' or not m['probe'].startswith('2'):
        fired['R1'] = ('CRITICAL',
                       f"网关宕机：workbuddy-web={m['service']} 容器={m['health']} 探活={m['probe'] or '无响应'}。"
                       '客户端会全部失败，请立即处理。')
    if m['timeout_n'] >= 3:
        wd = m.get('watchdog', '配置值')   # 实际看门狗（池侧 attempt_timeout_seconds）
        fired['R2'] = ('WARN',
                       f'上游僵死/劣化：最近{WINDOW_MIN}分钟 {m["timeout_n"]} 次账号尝试 {wd} 无响应头被强制换号。'
                       '典型表现：客户端变慢或超时；网关会自动换号，持续请关注。')
    ce = m.get('client', {})
    if (ce.get('total', 0) >= 3 or m['cap_n'] + m['pool_n'] >= 3 or m['s503_n'] >= 5):
        fired['R3'] = ('WARN',
                       f'客户端真实吃到拒绝 {ce.get("total", 0)} 次／最近{WINDOW_MIN}分钟'
                       f'（并发×{ce.get("concurrency", 0)} 配额×{ce.get("quota", 0)} '
                       f'容量×{ce.get("capacity", 0)} 池干涸×{ce.get("no_healthy", 0)} '
                       f'编码×{ce.get("encode", 0)}）；'
                       f'管理器侧 容量={m["cap_n"]} 池干涸={m["pool_n"]} 503={m["s503_n"]}。'
                       f'样例：{ce.get("sample", "无")}')
    bk = m.get('backup', {})
    if bk.get('age_h') is not None and bk['age_h'] > 26:
        fired['R5'] = ('WARN',
                       f'每日备份已 {bk["age_h"]:.1f} 小时未更新（阈值 26h）。'
                       '检查 /var/log/wb2api-backup.log 与 /root/wb2api-backup-push.sh。')
    elif bk.get('offsite') is False:
        fired['R6'] = ('INFO',
                       '每日备份正常，但**异地副本未配置**（offsite=false）：快照只躺在同一块盘上。'
                       + (f' 上次失败原因：{bk["error"]}' if bk.get('error') else
                          ' 配置 /root/backups/workbuddy-recovery/offsite.json 后生效。'))
    if m['soft_rate_n'] >= 5:
        fired['R4'] = ('INFO',
                       f'上游限流：最近{WINDOW_MIN}分钟 {m["soft_rate_n"]} 次「模型繁忙」429，'
                       '账号进入软限流冷却，吞吐会下降。')
    res = m.get('res', {})
    if res.get('swap_pct') is not None and res['swap_pct'] >= 60:
        fired['R7'] = ('WARN',
                       f"宿主机 swap 使用 {res['swap_pct']}%（{res['swap_used_mb']}MB）。"
                       '排查方向：僵尸/失控进程占内存、并发过高、大请求体。'
                       '命令：sudo python3 -c "import glob;[print(open(f).read().split(chr(10))[0]) for f in glob.glob(chr(47)+chr(112)+chr(114)+chr(111)+chr(99)+chr(47)+chr(42)+chr(47)+chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))]"')
    if res.get('zombies', 0) >= 3:
        fired['R8'] = ('WARN', f"存在 {res['zombies']} 个僵尸进程：父进程未回收子进程，检查 supervisor/systemd 配置。")
    if res.get('fat'):
        fired['R9'] = ('WARN', '非核心进程内存异常：' + '; '.join(res['fat'][:5]) + '（单进程 >1GB）')
    # WorkBuddy 风控增长（2026-10-22 审计后新增）：11-128 是「请求被渠道校验拦截」，
    # 也是上游识别第三方客户端的直接信号。单请求可能因换路径重试记 2-3 行，
    # 所以阈值取 5 次/窗口；持续越线说明规则变了，需要跟进改写或切备用上游。
    if m.get('risk_1128_n', 0) >= 5:
        fired['R10'] = ('CRITICAL',
                        f'WorkBuddy 风控拒绝 {m["risk_1128_n"]} 次（最近{WINDOW_MIN}分钟，code=11-128 渠道校验拦截）。'
                        '上游可能新增了客户端识别规则：先查 dev/upstream-patches/ 有无更高编号补丁；'
                        '持续出现则考虑扩大网关侧改写范围，或切到备用上游（zai-coding-cn）。')
    return fired


# ---------------------------------------------------------------- 通知渠道
def load_config() -> dict:
    default = {
        'wechat': {'enabled': True, 'botId': 'wx_8eff3e58c07fb792a8576ef5', 'targetId': '',
                   'api': 'http://127.0.0.1:3080/api/dsh-im/delivery/messages'},
        'email': {'enabled': False, 'smtp_host': 'smtp.qq.com', 'smtp_port': 465,
                  'user': '2421873411@qq.com', 'password': '', 'to': '2421873411@qq.com'},
    }
    try:
        cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return default
    for k, v in default.items():
        cfg.setdefault(k, v)
        for kk, vv in v.items():
            cfg[k].setdefault(kk, vv)
    return cfg


def send_wechat(cfg: dict, text: str) -> str:
    w = cfg['wechat']
    if not (w.get('enabled') and w.get('targetId')):
        return 'skip(未配置 targetId)'
    body = json.dumps({'botId': w['botId'], 'targetId': w['targetId'], 'text': text}).encode()
    req = urllib.request.Request(w['api'], data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = resp.read().decode()[:120]
            return 'ok' if '"sent": true' in out or '"sent":true' in out else f'unexpected: {out}'
    except Exception as exc:  # noqa: BLE001
        return f'fail: {exc}'


WEIXIN_SEND_URL = 'https://ilinkai.weixin.qq.com/ilink/bot/sendmessage'


def _weixin_token(cfg: dict) -> str:
    """从 DSH 凭据库按 tokenRef 取 bot token（密钥不落在告警配置里）。"""
    import re
    w = cfg.get('weixin_direct', {})
    ref = w.get('tokenRef') or ''
    path = w.get('credentials') or '/home/agentuser/.dsh/.credentials.yaml'
    if not ref:
        return ''
    try:
        text = Path(path).read_text(encoding='utf-8')
        found = re.search(re.escape(ref) + r'\s*:\s*(\S+)', text)
        return found.group(1) if found else ''
    except Exception:  # noqa: BLE001
        return ''


def send_weixin_direct(cfg: dict, text: str) -> str:
    """直连微信机器人平台发送。

    为什么不用 DSH 的 /api/dsh-im/delivery/messages：那条路要先在界面里建 target、
    且依赖 DSH 进程活着——告警恰恰最需要在 DSH 也出事时能用。这里直接打平台接口。
    """
    import base64
    import os
    w = cfg.get('weixin_direct', {})
    if not w.get('enabled'):
        return 'skip(未启用直连微信)'
    token = _weixin_token(cfg)
    if not token:
        return 'skip(取不到 bot token)'
    to_user = w.get('toUserId') or ''
    if not to_user:
        return 'skip(未配置 toUserId)'
    uin = base64.b64encode(str(int.from_bytes(os.urandom(4), 'big')).encode()).decode()
    body = json.dumps({
        'msg': {
            'from_user_id': '', 'to_user_id': to_user,
            'client_id': f'wb-gw-alert-{int(datetime.now().timestamp())}',
            'message_type': 2, 'message_state': 2,
            'item_list': [{'type': 1, 'text_item': {'text': text}}],
        },
        'base_info': {'channel_version': '2.4.6', 'bot_agent': 'WorkBuddyGatewayAlert/1.0'},
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(WEIXIN_SEND_URL, data=body, method='POST', headers={
        'Authorization': f'Bearer {token}',
        'AuthorizationType': 'ilink_bot_token',
        'X-WECHAT-UIN': uin,
        'iLink-App-Id': 'bot',
        'iLink-App-ClientVersion': str((2 << 16) | (4 << 8) | 6),
        'content-type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = resp.read().decode()[:200]
        return 'ok' if 'message_id' in out else f'unexpected: {out}'
    except Exception as exc:  # noqa: BLE001
        return f'fail: {exc}'


# ---------------------------------------------------------------- 邮件模板 v2
# 设计约束：邮件客户端剥离 <style>、拦远程图片、部分不支持 flex/grid。
# 因此全部用 table 布局 + 内联样式 + 字符/色块做视觉，不引任何外部资源。
# DeepSeek 品牌色板：主蓝 #4D6BFE，深空底 #0A0E27 → #1B2A6B
DS = {
    'blue':  '#4D6BFE',
    'blue2': '#6B84FF',
    'cyan':  '#22D3EE',
    'ink':   '#0A0E27',
    'deep':  '#131B3A',
    'deep2': '#1B2A6B',
    'paper': '#F7F9FC',
    'line':  '#E3E8F4',
    'muted': '#8A93B2',
    'text':  '#0F172A',
}


_LEVEL_STYLE = {
    'CRITICAL': {
        'c1': '#b91c1c', 'c2': '#7f1d1d', 'accent': '#ef4444',
        'icon': '🚨', 'label': 'CRITICAL', 'tag': '需立即处理', 'tint': '#fef2f2',
        'rule': '#fecaca',
    },
    'WARN': {
        'c1': '#c2740a', 'c2': '#92400e', 'accent': '#f59e0b',
        'icon': '⚠️', 'label': 'WARNING', 'tag': '需要关注', 'tint': '#fffbeb',
        'rule': '#fde68a',
    },
    'INFO': {
        'c1': '#1d4ed8', 'c2': '#1e3a8a', 'accent': '#3b82f6',
        'icon': 'ℹ️', 'label': 'INFO', 'tag': '仅供参考', 'tint': '#eff6ff',
        'rule': '#bfdbfe',
    },
    'RECOVER': {
        'c1': '#047857', 'c2': '#064e3b', 'accent': '#10b981',
        'icon': '✅', 'label': 'RECOVERED', 'tag': '已恢复正常', 'tint': '#ecfdf5',
        'rule': '#a7f3d0',
    },
}

# 指标名 → 健康状态（决定英雄数字的配色），没列出的按中性处理
_METRIC_ALERT = {'unhealthy', '无响应', 'fail', 'down', 'false', '异常'}


def _esc(v) -> str:
    return html.escape(str(v))


# ---------------------------------------------------------------- 邮件图形（PNG base64 内嵌）
# 为什么内嵌而不外链：邮件客户端默认拦远程图片，外链等于白做。
# 用 PIL 画 2x 图（retina 清晰），base64 塞进 data URI，不依赖任何网络。
import base64
import io
import math

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:  # noqa: BLE001  没装 PIL 时全部降级为纯 CSS
    _PIL_OK = False

_FONT_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
)


def _font(size: int):
    if not _PIL_OK:
        return None
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _png_data_uri(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _badge(level: str) -> str:
    """级别徽章：渐变小圆 + 几何符号（不依赖 emoji 字体）。"""
    if not _PIL_OK:
        return ''
    t = _LEVEL_STYLE.get(level.upper(), _LEVEL_STYLE['INFO'])
    S = 2                      # 2x 抗锯齿
    size = 96 * S
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 外发光
    for i in range(10, 0, -1):
        a = int(28 * (i / 10) ** 2)
        d.ellipse([i * 2 * S, i * 2 * S, size - i * 2 * S, size - i * 2 * S],
                  fill=_rgba(t['accent'], a))
    # 主圆（近似渐变：叠两层）
    d.ellipse([10 * S, 10 * S, size - 10 * S, size - 10 * S], fill=t['c1'])
    d.ellipse([18 * S, 18 * S, size - 26 * S, size - 26 * S], fill=_rgba('#ffffff', 26))
    cx = cy = size // 2
    lvl = level.upper()
    if lvl == 'RECOVER':
        # 对勾
        d.line([(cx - 20 * S, cy + 2 * S), (cx - 6 * S, cy + 16 * S)], fill='#ffffff', width=8 * S)
        d.line([(cx - 6 * S, cy + 16 * S), (cx + 22 * S, cy - 16 * S)], fill='#ffffff', width=8 * S)
    elif lvl == 'INFO':
        d.ellipse([cx - 4 * S, cy - 24 * S, cx + 4 * S, cy - 16 * S], fill='#ffffff')
        d.rounded_rectangle([cx - 4 * S, cy - 6 * S, cx + 4 * S, cy + 24 * S], radius=4 * S, fill='#ffffff')
    else:
        # 感叹号
        d.rounded_rectangle([cx - 4 * S, cy - 26 * S, cx + 4 * S, cy + 10 * S], radius=4 * S, fill='#ffffff')
        d.ellipse([cx - 4 * S, cy + 18 * S, cx + 4 * S, cy + 26 * S], fill='#ffffff')
    return _png_data_uri(img.resize((96, 96), Image.LANCZOS))


def _rgba(hex_color: str, alpha: int):
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, alpha)


def _health_ring(ok: int, total: int, level: str) -> str:
    """健康度环：正常项占比，中心写 ok/total。"""
    if not _PIL_OK or total <= 0:
        return ''
    t = _LEVEL_STYLE.get(level.upper(), _LEVEL_STYLE['INFO'])
    S = 2
    size = 140 * S
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 12 * S
    box = [pad, pad, size - pad, size - pad]
    d.arc(box, 0, 360, fill=_rgba('#e2e8f0', 255), width=14 * S)
    ratio = max(0.0, min(1.0, ok / total))
    color = '#10b981' if ratio >= 0.9 else ('#f59e0b' if ratio >= 0.5 else '#ef4444')
    if ratio > 0:
        d.arc(box, -90, -90 + 360 * ratio, fill=color, width=14 * S)
    f_big = _font(30 * S)
    f_small = _font(11 * S)
    txt = f'{ok}/{total}'
    d.text((size // 2, size // 2 - 6 * S), txt, font=f_big, fill='#0f172a', anchor='mm')
    d.text((size // 2, size // 2 + 18 * S), 'HEALTHY', font=f_small, fill='#94a3b8', anchor='mm')
    return _png_data_uri(img.resize((140, 140), Image.LANCZOS))


def _metric_bars(rows: list, level: str) -> str:
    """横向条形图：每行一个指标，色块长度=相对值。rows=[(label, value, ratio, color)]"""
    if not _PIL_OK or not rows:
        return ''
    S = 2
    W, H = 560 * S, (len(rows) * 34 + 8) * S
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _font(12 * S)
    for i, (label, value, ratio, color) in enumerate(rows):
        y = (i * 34 + 6) * S
        d.text((0, y - 2 * S), label, font=f, fill='#64748b')
        right = f'{value}'
        d.text((W - d.textlength(right, font=f), y - 2 * S), right, font=f, fill='#0f172a')
        bar_y = y + 18 * S
        d.rounded_rectangle([0, bar_y, W, bar_y + 8 * S], radius=4 * S, fill=_rgba('#eef2f7', 255))
        w = max(6 * S, int(W * max(0.02, min(1.0, ratio))))
        d.rounded_rectangle([0, bar_y, w, bar_y + 8 * S], radius=4 * S, fill=color)
    return _png_data_uri(img.resize((560, H // S), Image.LANCZOS))


def _hero_numbers(metrics: dict) -> str:
    """把「数字型」指标提出来做英雄区：大号数字 + 小标签。"""
    picks = []
    for label, value in metrics.items():
        s = str(value)
        if s.replace('.', '', 1).isdigit() and len(s) <= 6:
            picks.append((label, s))
        if len(picks) >= 3:
            break
    if not picks:
        return ''
    cells = []
    for i, (label, val) in enumerate(picks):
        pad = '0 20px 0 0' if i < len(picks) - 1 else '0'
        cells.append(
            '<td style="padding:' + pad + ';vertical-align:top">'
            '<div style="font-size:34px;font-weight:800;color:#0f172a;line-height:1;'
            "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif\">"
            + _esc(val) + '</div>'
            '<div style="font-size:11px;color:#64748b;margin-top:7px;letter-spacing:.6px">'
            + _esc(label) + '</div></td>'
        )
    return ('<table cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 4px"><tr>'
            + ''.join(cells) + '</tr></table>')


def _state_rows(metrics: dict) -> str:
    """把「状态型」指标渲染成带色块的横条，比纯文字更易扫读。"""
    rows = []
    for label, value in metrics.items():
        s = str(value)
        if s.replace('.', '', 1).isdigit() and len(s) <= 6:
            continue  # 数字型已在英雄区
        bad = s.lower() in _METRIC_ALERT
        good = s.lower() in {'active', 'healthy', '200', 'ok', 'true', '正常'}
        dot = '#ef4444' if bad else ('#10b981' if good else '#94a3b8')
        rows.append(
            '<tr>'
            '<td style="padding:7px 0;font-size:13px;color:#64748b;width:38%">' + _esc(label) + '</td>'
            '<td style="padding:7px 0;font-size:13px;color:#0f172a;font-weight:600">'
            '<span style="display:inline-block;width:7px;height:7px;border-radius:4px;'
            'background:' + dot + ';margin-right:8px;vertical-align:middle"></span>'
            + _esc(s) + '</td></tr>'
        )
    if not rows:
        return ''
    return ('<div style="font-size:11px;color:#94a3b8;letter-spacing:1.5px;font-weight:600;'
            'margin:22px 0 6px">状态</div>'
            '<table cellpadding="0" cellspacing="0" border="0" width="100%">'
            + ''.join(rows) + '</table>')


def _advice_block(advice: str, t: dict) -> str:
    if not advice:
        return ''
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin-top:22px;background:' + t['tint'] + ';'
        'border-left:3px solid ' + t['accent'] + ';border-radius:0 8px 8px 0">'
        '<tr><td style="padding:14px 16px">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:1.4px;color:' + t['c1'] + ';'
        'margin-bottom:7px">▸ 建议动作</div>'
        '<div style="font-size:13px;line-height:1.85;color:#334155">' + advice + '</div>'
        '</td></tr></table>'
    )


# ---------------------------------------------------------------- 邮件资产（AI 生成图）
# 关键：QQ / 163 邮箱会屏蔽 data: URI 图片（安全策略），必须用 CID 内嵌——
# 图片作为 multipart/related 的独立 part，HTML 里用 <img src="cid:xxx"> 引用，
# 客户端直接从邮件体取，不联网、不被拦。
_ASSET_DIR = Path('/var/www/mail-assets')
_RENDER_IMAGES: dict = {}     # cid → (mime, bytes)，每次渲染前清空


def _asset_cid(name: str) -> str:
    """登记一张资产并返回 cid: 引用；文件缺失返回空串。"""
    if not name:
        return ''
    path = _ASSET_DIR / name
    if not path.is_file():
        return ''
    cid = name.rsplit('.', 1)[0].replace('.', '-')
    if cid not in _RENDER_IMAGES:
        try:
            _RENDER_IMAGES[cid] = (path.read_bytes(), name)
        except Exception:  # noqa: BLE001
            return ''
    return 'cid:' + cid


_MAIL_BASE_URL = 'https://dsh.kaogong.art/mail-assets'


def _asset_url(name: str) -> str:
    """邮件里用 HTTPS 外链（QQ/163 对 data URI 与 CID 都不稳定）。
    图片由 nginx 的 /mail-assets/ 直接提供，auth_request off，无需 cookie。"""
    if not name:
        return ''
    if not (_ASSET_DIR / name).is_file():
        return ''
    return _MAIL_BASE_URL + '/' + name


def _asset_data_uri(name: str) -> str:
    """data URI 版本，仅用于预览渲染（发信时走 CID）。"""
    path = _ASSET_DIR / name
    if not path.is_file():
        return ''
    mime = 'image/jpeg' if name.lower().endswith(('.jpg', '.jpeg')) else 'image/png'
    return 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


_BADGE_FILE = {
    'CRITICAL': 'badge-critical.png',
    'WARN': 'badge-warn.png',
    'INFO': 'badge-info.png',
    'RECOVER': 'badge-recover.png',
}



def _render_analysis(analysis: dict | None, level: str) -> str:
    """把模型返回的分段文本渲染成「AI 诊断」区块。"""
    if not analysis or not analysis.get('ok') or not analysis.get('text'):
        return ''
    t = _LEVEL_STYLE.get(level.upper(), _LEVEL_STYLE['INFO'])
    raw = analysis['text']
    model = analysis.get('model', 'AI')
    # 按 [标题] 切段
    parts = []
    cur_title, cur_body = '', []
    for line in raw.split('\n'):
        stripped = line.strip()
        if stripped.startswith('[') and ']' in stripped:
            if cur_title or cur_body:
                parts.append((cur_title, '\n'.join(cur_body).strip()))
            cur_title = stripped[:stripped.index(']') + 1]
            cur_body = [stripped[stripped.index(']') + 1:].strip()]
        else:
            cur_body.append(line)
    if cur_title or cur_body:
        parts.append((cur_title, '\n'.join(cur_body).strip()))

    rows = []
    for title, body in parts:
        if not body and not title:
            continue
        body_html = html.escape(body).replace('\n', '<br>')
        rows.append(
            '<tr><td style="padding:10px 0 0;vertical-align:top;width:64px">'
            '<span style="display:inline-block;font-size:10.5px;font-weight:700;color:'
            + t['accent'] + ';letter-spacing:.5px">' + _esc(title.strip('[]')) + '</span></td>'
            '<td style="padding:10px 0 0;font-size:13px;line-height:1.8;color:#334155">'
            + body_html + '</td></tr>')
    if not rows:
        rows = ['<tr><td style="font-size:13px;line-height:1.8;color:#334155">'
                + html.escape(raw).replace('\n', '<br>') + '</td></tr>']

    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin-top:22px;background:#f8fafc;border:1px solid ' + DS['line'] + ';'
        'border-radius:10px"><tr><td style="padding:14px 16px 16px">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:1.4px;color:' + DS['blue'] + ';'
        'margin-bottom:4px">AI 诊断 <span style="font-weight:500;color:#94a3b8">· '
        + _esc(model) + '</span></div>'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%">'
        + ''.join(rows) + '</table>'
        '</td></tr></table>')


def _email_html(subject: str, text: str, level: str = 'INFO',
                metrics: dict | None = None, advice: str = '',
                analysis: dict | None = None) -> str:
    t = _LEVEL_STYLE.get(level.upper(), _LEVEL_STYLE['INFO'])
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    host = socket.gethostname()
    head_sub = subject.split(' ', 1)[-1] if ' ' in subject else subject
    metrics = metrics or {}
    body_html = html.escape(text).replace('\n', '<br>')

    header_uri = _asset_url('header.jpg')
    badge_uri = _asset_url(_BADGE_FILE.get(level.upper(), ''))

    GOOD = {'active', 'healthy', '200', 'ok', 'true', '正常', '已恢复'}
    BAD = {'unhealthy', '无响应', 'fail', 'down', 'false', '异常', '失败'}

    rows = []
    for label, value in metrics.items():
        s_val = str(value)
        low = s_val.lower()
        color = '#ef4444' if low in BAD else ('#10b981' if low in GOOD else '#94a3b8')
        rows.append(
            '<tr><td style="padding:7px 0;font-size:13px;color:#64748b">' + _esc(label) + '</td>'
            '<td style="padding:7px 0;font-size:13px;color:#0f172a;font-weight:600;text-align:right">'
            + _esc(s_val) + '<span style="display:inline-block;width:8px;height:8px;border-radius:4px;'
            'background:' + color + ';margin-left:9px;vertical-align:middle"></span></td></tr>')
    state_tbl = ('<table cellpadding="0" cellspacing="0" border="0" width="100%">'
                 + ''.join(rows) + '</table>') if rows else ''

    badge_td = ''
    if badge_uri:
        badge_td = ('<td style="width:96px;vertical-align:top;padding-right:18px">'
                    '<img src="' + badge_uri + '" width="88" height="88" alt="' + level + '" '
                    'style="display:block;border:0;outline:none"></td>')
    two_col = ('<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
               + badge_td +
               '<td style="vertical-align:middle">'
               '<div style="font-size:11px;color:#94a3b8;letter-spacing:1.5px;font-weight:600;'
               'margin-bottom:8px">当前状态</div>' + state_tbl + '</td></tr></table>'
               ) if (badge_td or state_tbl) else ''

    nums = [(k, str(v)) for k, v in metrics.items() if str(v).replace('.', '', 1).isdigit()]
    bars = ''
    if nums:
        mx = max((float(v) for _, v in nums), default=1.0) or 1.0
        palette = [t['accent'], DS['cyan'], DS['blue2'], '#8b5cf6']
        bar_rows = [(k, v, float(v) / mx, palette[i % len(palette)]) for i, (k, v) in enumerate(nums)]
        uri = _metric_bars(bar_rows, level)
        if uri:
            # PIL 生成的是 data URI；落盘到静态目录后改用外链
            try:
                data = base64.b64decode(uri.split(',', 1)[1])
                target = _ASSET_DIR / 'metrics-bars.png'
                target.write_bytes(data)
                try:
                    target.chmod(0o644)
                except Exception:  # noqa: BLE001
                    pass
                src = _MAIL_BASE_URL + '/metrics-bars.png'
            except Exception:  # noqa: BLE001
                src = uri
            bars = ('<div style="font-size:11px;color:#94a3b8;letter-spacing:1.5px;'
                    'font-weight:600;margin:24px 0 10px">关键计数</div>'
                    '<img src="' + src + '" width="560" alt="metrics" '
                    'style="display:block;border:0;outline:none;max-width:100%">')

    advice_html = ''
    if advice:
        advice_html = (
            '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
            'style="margin-top:24px;background:' + t['tint'] + ';border-left:3px solid '
            + t['accent'] + ';border-radius:0 8px 8px 0"><tr><td style="padding:14px 16px">'
            '<div style="font-size:11px;font-weight:700;letter-spacing:1.4px;color:'
            + DS['blue'] + ';margin-bottom:7px">▸ 建议动作</div>'
            '<div style="font-size:13px;line-height:1.85;color:#334155">' + advice + '</div>'
            '</td></tr></table>')

    # 头部：AI 生成的鲸鱼横幅 + 深色文字（DeepSeek 官网的浅蓝底深字）
    if header_uri:
        header = (
            '<tr><td background="' + header_uri + '" '
            'style="background-image:url(' + header_uri + ');background-size:cover;'
            'background-position:center right;border-radius:16px 16px 0 0;padding:36px 30px 32px">'
            '<div style="font-size:10.5px;color:' + DS['blue'] + ';letter-spacing:3px;'
            'font-weight:700;text-shadow:0 1px 0 rgba(255,255,255,.6)">' + t['label'] + '</div>'
            '<div style="font-size:26px;font-weight:800;color:' + DS['ink'] + ';margin-top:8px;'
            'line-height:1.25;letter-spacing:-.4px;text-shadow:0 1px 2px rgba(255,255,255,.55)">'
            + _esc(head_sub) + '</div>'
            '<div style="font-size:12.5px;color:' + DS['deep2'] + ';margin-top:10px;font-weight:600;'
            'text-shadow:0 1px 0 rgba(255,255,255,.5)">' + _esc(t['tag'])
            + ' &nbsp;·&nbsp; WorkBuddy Gateway</div>'
            '</td></tr>')
    else:
        header = (
            '<tr><td style="background:' + DS['deep2'] + ';border-radius:16px 16px 0 0;'
            'padding:30px 28px">'
            '<div style="font-size:10.5px;color:rgba(255,255,255,.7);letter-spacing:3px;'
            'font-weight:700">' + t['label'] + '</div>'
            '<div style="font-size:26px;font-weight:800;color:#fff;margin-top:9px;'
            'line-height:1.25">' + _esc(head_sub) + '</div>'
            '<div style="font-size:12px;color:rgba(255,255,255,.78);margin-top:9px">'
            + _esc(t['tag']) + ' &nbsp;·&nbsp; WorkBuddy Gateway</div>'
            '</td></tr>')

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#eef2f7">'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="background:#eef2f7;padding:28px 12px"><tr><td align="center">'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:600px">'
        + header +
        '<tr><td style="background:#fff;padding:0 28px">'
        '<div style="font-size:11px;color:#94a3b8;padding:10px 0 0">'
        '若未显示图片/徽章，点邮件客户端顶部「显示图片」即可</div></td></tr>'
        '<tr><td style="background:#fff;padding:18px 28px 26px;'
        'border-left:1px solid ' + DS['line'] + ';border-right:1px solid ' + DS['line'] + '">'
        + two_col +
        '<div style="font-size:11px;color:#94a3b8;letter-spacing:1.5px;font-weight:600;'
        'margin:24px 0 9px">告警详情</div>'
        '<div style="font-size:14px;color:#1e293b;line-height:1.85">' + body_html + '</div>'
        + bars + _render_analysis(analysis, level) + advice_html +
        '</td></tr>'
        '<tr><td style="background:#f8fafc;border:1px solid ' + DS['line'] + ';border-top:none;'
        'border-radius:0 0 16px 16px;padding:16px 28px">'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>'
        '<td style="font-size:11.5px;color:#94a3b8;line-height:1.9">'
        '<b style="color:#64748b">' + _esc(host) + '</b> &nbsp;·&nbsp; ' + stamp + '<br>'
        'wb-gateway-alert.py &nbsp;·&nbsp; 巡检周期 5 分钟 &nbsp;·&nbsp; 自动发送，无需回复'
        '</td>'
        '<td style="text-align:right;font-size:11px;color:#cbd5e1;font-weight:600;'
        'letter-spacing:1px">WORKBUDDY</td>'
        '</tr></table></td></tr>'
        '</table></td></tr></table></body></html>'
    )


def send_email(cfg: dict, subject: str, text: str, level: str = 'INFO',
               metrics: dict | None = None, advice: str = '',
               analysis: dict | None = None) -> str:
    e = cfg['email']
    if not (e.get('enabled') and e.get('password')):
        return 'skip(未配置 SMTP 授权码)'
    # 收件人支持逗号/分号/空格分隔的多地址
    raw_to = e.get('to') or ''
    if isinstance(raw_to, (list, tuple)):
        recipients = [str(x).strip() for x in raw_to if str(x).strip()]
    else:
        recipients = [x.strip() for x in str(raw_to).replace(';', ',').replace(' ', ',').split(',') if x.strip()]
    if not recipients:
        return 'skip(未配置收件人)'
    try:
        _RENDER_IMAGES.clear()
        html_body = _email_html(subject, text, level, metrics, advice, analysis)
        images = dict(_RENDER_IMAGES)          # cid -> (bytes, filename)

        msg = MIMEMultipart('related')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = formataddr((str(Header('WorkBuddy 网关告警', 'utf-8')), e['user']))
        msg['To'] = ', '.join(recipients)

        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(text, 'plain', 'utf-8'))
        alt.attach(MIMEText(html_body, 'html', 'utf-8'))
        msg.attach(alt)

        for cid, (data, filename) in images.items():
            sub = MIMEImage(data, _subtype='jpeg' if filename.lower().endswith(('.jpg', '.jpeg')) else 'png')
            sub.add_header('Content-ID', '<' + cid + '>')
            sub.add_header('Content-Disposition', 'inline', filename=filename)
            msg.attach(sub)

        with smtplib.SMTP_SSL(e['smtp_host'], e.get('smtp_port', 465), timeout=25) as s:
            s.login(e['user'], e['password'])
            s.sendmail(e['user'], recipients, msg.as_string())
        return 'ok'
    except Exception as exc:  # noqa: BLE001
        return f'fail: {exc}'


# ---------------------------------------------------------------- AI 告警分析
# 降级链：DeepSeek V4.1（走本机 WorkBuddy 网关）→ GLM-5.3-Flash（直连智谱）。
# 刻意让第一条依赖被监控链路：网关挂了时它会失败，正好验证降级是否生效。
_ANALYZER_PROMPT = '''你是 WorkBuddy 网关的运维专家。收到一条自动告警，请给出可执行的诊断。

要求：
- 用中文，总长控制在 300 字以内，不要客套话。
- 只依据给定信息推断；信息不足时明确说"信息不足，建议先查 X"，不要编造。
- 判断要具体到操作：给出可直接粘贴的命令，不要泛泛而谈。

输出严格用下面四段，每段以方括号标题开头（不要加 markdown 的 #）：

[判断] 一句话：这是真故障 / 误报 / 待观察，紧急程度如何
[根因] 最可能的 1-2 个成因，按可能性排序
[处置] 编号列出 2-3 条具体命令或动作，含命令原文
[风险] 不处理的后果，以及什么信号出现时要升级处理
'''

_LLM_CHAIN = (
    {
        'name': 'DeepSeek-V4.1',
        'base': 'http://127.0.0.1:7864/v1',
        'model': 'deepseek-v4.1-flash',
        'key_source': 'gateway',
        'timeout': 60,
    },
    {
        'name': 'GLM-5.3-Flash',
        'base': 'https://open.bigmodel.cn/api/coding/paas/v4',
        'model': 'glm-5.3-flash',
        'key_source': 'zai',
        'timeout': 60,
    },
)


def _gateway_api_key() -> str:
    """从告警配置或 DSH 凭据里取本机网关的 wbk_ 钥匙。"""
    try:
        cfg = load_config()
        k = (cfg.get('analyzer') or {}).get('gateway_key')
        if k:
            return k
    except Exception:  # noqa: BLE001
        pass
    try:
        import yaml  # type: ignore
        cred = yaml.safe_load(Path('/home/agentuser/.dsh/.credentials.yaml').read_text(encoding='utf-8'))
        return (cred.get('records', {}).get('llm-pi-ai/zhihui', {}) or {}).get('key', '')
    except Exception:  # noqa: BLE001
        return ''


def _zai_api_key() -> str:
    try:
        import yaml  # type: ignore
        cred = yaml.safe_load(Path('/home/agentuser/.dsh/.credentials.yaml').read_text(encoding='utf-8'))
        return (cred.get('refs', {}) or {}).get('ZAI_CODING_CN_API_KEY', '')
    except Exception:  # noqa: BLE001
        return ''


def _llm_call(step: dict, system: str, user: str) -> str:
    key = _gateway_api_key() if step['key_source'] == 'gateway' else _zai_api_key()
    if not key:
        raise RuntimeError('missing key for ' + step['name'])
    body = json.dumps({
        'model': step['model'],
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': 0.2,
        'max_tokens': 2000,
        'stream': False,
    }).encode()
    req = urllib.request.Request(
        step['base'].rstrip('/') + '/chat/completions', data=body,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
        method='POST')
    with urllib.request.urlopen(req, timeout=step['timeout']) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    msg = (data.get('choices') or [{}])[0].get('message') or {}
    content = (msg.get('content') or '').strip()
    if not content:
        # 思考型模型（如 GLM-5.3 系列在部分档位）会把整段输出放进 reasoning_content，
        # content 留空。此时用 reasoning_content 兜底，否则降级链会误判为失败。
        content = (msg.get('reasoning_content') or '').strip()
    if not content:
        finish = (data.get('choices') or [{}])[0].get('finish_reason', '?')
        raise RuntimeError(f'empty content from {step["name"]} (finish_reason={finish})')
    return content


def analyze_alert(rule: str, level: str, detail: str, metrics: dict) -> dict:
    """返回 {ok, model, text, error}；全部失败时 ok=False，邮件退回纯模板。"""
    user = (
        f'告警规则：{rule}（级别 {level}）\n'
        f'告警内容：{detail}\n'
        f'当前指标：{json.dumps(metrics, ensure_ascii=False)}\n'
        f'主机：{socket.gethostname()}\n'
        f'时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n'
    )
    errors = []
    for step in _LLM_CHAIN:
        try:
            text = _llm_call(step, _ANALYZER_PROMPT, user)
            log(f'ANALYZE {rule} via {step["name"]}: ok ({len(text)} 字)')
            return {'ok': True, 'model': step['name'], 'text': text, 'error': ''}
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{step["name"]}: {type(exc).__name__} {str(exc)[:80]}')
            log(f'ANALYZE {rule} via {step["name"]}: fail {exc}')
    return {'ok': False, 'model': '', 'text': '', 'error': ' | '.join(errors)}


# ---------------------------------------------------------------- 邮件内容辅助
_RULE_ADVICE = {
    'R1': '立即确认：<code>systemctl status workbuddy-web</code> 与 '
          '<code>docker ps --filter name=workbuddy2api</code>。'
          '若进程已退出，看 <code>journalctl -u workbuddy-web -n 50</code> 的最后异常。',
    'R2': '上游反复 attempt timeout：检查 <code>docker logs workbuddy2api --since 20m</code>，'
          '关注是否集中在同一账号；持续出现考虑该账号临时停用。',
    'R3': '客户端吃到拒绝：看本轮并发/配额/容量明细，必要时下调 '
          'WB_GATEWAY_MAX_CONCURRENCY 或检查密钥配额。',
    'R4': '上游限流 429：账号进入软冷却，吞吐会下降，通常几分钟自愈；'
          '频繁出现说明账号池偏小或集中在高峰。',
    'R5': '每日备份超 26 小时未更新：查 <code>/var/log/wb2api-backup.log</code>，'
          '手动跑一次 <code>/root/wb2api-backup-push.sh</code> 看报错。',
    'R6': '离线副本未配置：把 recovery-public.pem 放进 '
          '<code>/root/backups/workbuddy-recovery/</code> 并写 offsite.json。',
    'R7': 'swap 偏高：先找吃内存的进程（<code>ps -eo rss,comm --sort=-rss | head</code>），'
          '排查僵尸/失控进程与并发峰值。',
    'R8': '存在僵尸进程：父进程未回收子进程，检查对应 supervisor 或 systemd 单元。',
    'R9': '单进程内存超 1GB：确认是否正常业务进程，异常的话采样 <code>py-spy</code>/堆栈后重启。',
    'R10': 'WorkBuddy 风控拒绝 11-128：先查 dev/upstream-patches/ 有无更高编号补丁；'
           '持续出现考虑扩大网关侧改写范围，或切到备用上游（zai-coding-cn）。',
}


def _email_metrics(m: dict) -> dict:
    out = {
        '服务': m.get('service', '?'),
        '容器': m.get('health', '?') or '?',
        '探活': m.get('probe', '?') or '?',
    }
    if m.get('timeout_n'):
        out['超时轮换'] = m['timeout_n']
    if m.get('s503_n'):
        out['503'] = m['s503_n']
    if m.get('risk_1128_n'):
        out['风控拒绝'] = m['risk_1128_n']
    res = m.get('res') or {}
    if res.get('swap_pct') is not None:
        out['swap'] = f"{res['swap_pct']}%"
    return out


# ---------------------------------------------------------------- 主流程
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')


def _onfailure_main(unit: str) -> int:
    """systemd OnFailure 触发的即时告警：不跑巡检，直接发一封信。"""
    cfg = load_config()
    host = socket.gethostname()
    status = sh(f'systemctl show {unit} -p ActiveState -p SubState -p Result -p NRestarts 2>/dev/null')
    journal_tail = sh(f'journalctl -u {unit} -n 12 --no-pager -q 2>/dev/null')
    text = (
        f'{unit} 非正常退出，systemd 已通过 OnFailure 立即触发本告警。\n\n'
        f'状态：\n{status.strip()}\n\n'
        f'最近日志（最后 12 行）：\n{journal_tail.strip()[-1200:]}\n\n'
        f'主机 {host} 会自动尝试拉起（Restart=always）；若连续失败，检查 journalctl -u {unit}。'
    )
    advice = ('先看状态里的 Result 字段：exit-code / signal / timeout 含义不同。'
              '若 NRestarts 在快速上涨，说明起不来，先 systemctl stop 再排查，避免重启风暴。')
    m = {}
    for line in status.splitlines():
        if '=' in line:
            k, _, v = line.partition('=')
            if k.strip() in ('ActiveState', 'SubState', 'Result', 'NRestarts'):
                m[k.strip()] = v.strip()
    r = send_email(cfg, f'[CRITICAL] {unit} 异常退出', text,
                   level='CRITICAL', metrics=m, advice=advice)
    log(f'ONFAILURE {unit}: email={r}')
    return 0


def main() -> int:
    if '--onfailure' in sys.argv:
        idx = sys.argv.index('--onfailure')
        unit = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else 'workbuddy-web.service'
        try:
            return _onfailure_main(unit)
        except Exception as exc:  # noqa: BLE001  兜底：绝不让告警本身抛错
            log(f'ONFAILURE 处理失败: {exc}')
            return 0
    cfg = load_config()
    state = load_state()
    m = collect()
    fired = evaluate(m)
    now = datetime.now().timestamp()
    lines = []

    # 上游容器自愈（保守）：连续 3 次巡检发现 unhealthy 才重建一次，且每小时最多一次。
    # 只对「容器不健康」生效——web 服务本身异常不在这里自动重启，避免掩盖配置问题。
    try:
        if m.get('health') not in (None, 'healthy', '') and m.get('service') == 'active':
            st = state.setdefault('_selfheal', {'streak': 0, 'last': 0})
            st['streak'] = int(st.get('streak', 0)) + 1
            if st['streak'] >= 3 and now - int(st.get('last', 0)) > 3600:
                subprocess.run(['docker', 'restart', 'workbuddy2api'],
                               capture_output=True, timeout=90)
                st['last'] = now
                st['streak'] = 0
                lines.append(f'容器自愈：连续 3 次 unhealthy，已执行 docker restart workbuddy2api')
        else:
            state.setdefault('_selfheal', {'streak': 0, 'last': 0})['streak'] = 0
    except Exception as exc:  # noqa: BLE001  自愈失败不影响告警主流程
        lines.append(f'容器自愈失败: {exc}')

    for rule, (level, text) in fired.items():
        last = state.get(rule, {}).get('ts', 0)
        cool = RULE_COOLDOWN_MIN.get(rule, COOLDOWN_MIN) * 60
        if now - last < cool and state.get(rule, {}).get('level') == level:
            lines.append(f'{rule} 触发但在冷却期，跳过')
            continue
        stamp = f'{datetime.now():%m-%d %H:%M}'
        subject = f'[{level}] WorkBuddy网关告警 {rule} {stamp}'
        full = f'{subject}\n{text}'
        wx = send_weixin_direct(cfg, full)
        we = send_wechat(cfg, full) if wx != 'ok' else 'skip(直连微信已送达)'
        # 只有 CRITICAL / WARN 才值得让模型分析：INFO 类是「仅供参考」，
        # 调用一次要 5-16 秒 + 几百 token，不划算。
        analysis = None
        if level in ('CRITICAL', 'WARN'):
            try:
                analysis = analyze_alert(rule, level, text, _email_metrics(m))
            except Exception as exc:  # noqa: BLE001  分析失败不影响发信
                log(f'ANALYZE {rule} 异常: {exc}')
        em = (send_email(cfg, subject, full, level=level, metrics=_email_metrics(m),
                        advice=_RULE_ADVICE.get(rule, ''), analysis=analysis)
              if (wx != 'ok' and we != 'ok') else 'skip(已有渠道送达)')
        if wx != 'ok' and we != 'ok' and em != 'ok':
            # 没有任何送达渠道：绝不能静默——落盘 + 明确标注，等下次配置好即可复盘
            try:
                FALLBACK.parent.mkdir(parents=True, exist_ok=True)
                with FALLBACK.open('a', encoding='utf-8') as fh:
                    fh.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {full}\n')
            except OSError:
                pass
            lines.append(f'{rule} 已触发(level={level}) **未送达** 微信直连={wx} dsh_im={we} email={em}'
                         f'（正文已追加到 {FALLBACK}）')
        else:
            lines.append(f'{rule} 已告警(level={level}) 微信直连={wx} dsh_im={we} email={em}')
        state[rule] = {'ts': now, 'level': level, 'firing': True}

    # 恢复通知：上一轮在烧、这轮干净
    for rule in list(state):
        if rule in fired or not state[rule].get('firing'):
            continue
        if state[rule].get('ts', 0) and now - state[rule]['ts'] > COOLDOWN_MIN * 60 * 2:
            continue
        stamp = f'{datetime.now():%m-%d %H:%M}'
        full = f'[已恢复] WorkBuddy网关 {rule} {stamp}\n指标已回到正常区间。'
        wx = send_weixin_direct(cfg, full)
        we = send_wechat(cfg, full) if wx != 'ok' else 'skip(直连微信已送达)'
        em = (send_email(cfg, f'[已恢复] WorkBuddy网关 {rule}', full,
                        level='RECOVER', metrics=_email_metrics(m))
              if wx != 'ok' else 'skip(已有渠道送达)')
        state[rule]['firing'] = False
        lines.append(f'{rule} 已恢复通知 微信直连={wx} dsh_im={we} email={em}')

    save_state(state)
    if not lines:
        lines.append(f'ok(无告警: timeout={m["timeout_n"]} cap={m["cap_n"]} pool={m["pool_n"]} '
                     f'503={m["s503_n"]} soft={m["soft_rate_n"]} probe={m["probe"]} '
                     f'svc={m["service"]}/{m["health"]})')
    for ln in lines:
        log(ln)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
