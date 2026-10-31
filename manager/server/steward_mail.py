"""Small, script-free HTML mail and explicit SMTP acceptance semantics."""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
import html

from .notify import redact
from .services import smtp_transport


def render(payload, analysis=None):
    event = payload.get('event')
    recovered = event == 'recovered'
    label = '已恢复' if recovered else '通知测试' if event == 'test' else '管家研判' if event == 'analysis' else '需要关注'
    accent = '#168574' if recovered else '#bf6a24' if payload.get('severity') == 'critical' else '#5278bc'
    title = redact(payload.get('title', '网关状态有变化'))
    detail = redact(payload.get('detail', '暂无详细证据'))
    stamp = datetime.fromtimestamp(payload['observed_at'], timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')
    analysis = analysis or payload.get('analysis')
    answer = redact(analysis, limit=3000) if analysis else ''
    text = f'智汇管家 · {label}\n{title}\n\n已核实的信号：{detail}\n观察时间：{stamp}\n'
    if answer:
        text += f'\nAI 辅助分析（不是已证实根因）：\n{answer}\n'
    else:
        text += '\n依据规则先行通知，不等待 AI 诊断；如已启用研判，可在完成后收到独立补充。\n'
    text += '\n请在智汇工作台核对状态。邮件不会索取密码、授权码或 API 密钥。'
    esc = lambda value: html.escape(str(value), quote=True)
    diagnosis = (f'<p style="white-space:pre-wrap;line-height:1.85;color:#45566e">{esc(answer)}</p>' if answer else
                 '<p style="color:#718197;line-height:1.8">规则提醒先送达，不等待 AI 诊断。已启用的研判完成后，将独立补充。</p>')
    body = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;background:#f6f9fc;color:#243249;font-family:'Microsoft YaHei',sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:30px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px;background:#ffffff;border:1px solid #e3eaf3;border-radius:24px">
<tr><td style="padding:30px 30px 22px;border-bottom:1px solid #edf1f7"><span style="font-size:28px;font-weight:800;color:#6c89da">Z</span>
<span style="font-size:17px;font-weight:700;padding-left:12px">智汇管家</span><p style="margin:8px 0 0;font-size:10px;letter-spacing:2px;color:#8998ac">ZHIHUI / ALWAYS ATTENTIVE</p></td></tr>
<tr><td style="padding:28px 30px"><span style="color:{accent};font-size:12px">● {label}</span>
<h1 style="font-size:24px;line-height:1.5;margin:12px 0 18px">{esc(title)}</h1>
<div style="background:#f8fafc;border:1px solid #e9eef5;border-radius:16px;padding:18px">
<p style="font-size:11px;color:#8090a4;margin:0 0 8px">已核实的信号</p><p style="line-height:1.85;margin:0;white-space:pre-wrap">{esc(detail)}</p></div>
<h2 style="font-size:13px;margin:24px 0 4px">管家研判 <span style="font-weight:400;color:#8d9aaa">/ AI 辅助，不替代事实</span></h2>{diagnosis}
<a href="https://zhihui.kaogong.art/settings/alerts/" style="display:inline-block;background:#26364e;color:#fff;border-radius:24px;padding:12px 22px;text-decoration:none;font-size:12px;margin:12px 0">在工作台查看 →</a>
<p style="font-size:11px;color:#8593a7;line-height:1.8">{esc(stamp)} · 智汇网关<br>不索取密码，不附带密钥。只在状态变化时提醒。</p></td></tr></table>
<p style="font-size:10px;color:#94a1b1;letter-spacing:1px">少一点打扰，多一分笃定。</p></td></tr></table></body></html>'''
    return {'subject': f'[{label}] 智汇管家 · {title}'[:150], 'text': text, 'html': body}


def send(settings, recipient, payload, *, delivery_id, analysis=None, smtp_factory=None):
    rendered = render(payload, analysis)
    message = EmailMessage()
    message['Subject'] = rendered['subject']
    message['From'] = formataddr(('智汇管家', settings['username']))
    message['To'] = recipient
    message['Date'] = formatdate(localtime=False)
    message['Message-ID'] = f'<zhihui-steward-{delivery_id}@zhihui.kaogong.art>'
    message.set_content(rendered['text'])
    message.add_alternative(rendered['html'], subtype='html')
    return smtp_transport.send(settings, recipient, message.as_bytes(), smtp_factory=smtp_factory)
