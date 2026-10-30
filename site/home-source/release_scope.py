"""Select the shipping three-platform scope without changing the supplied snapshot."""
import re


def three_platforms(html):
    for attribute in ('platform', 'hero-platform', 'route', 'code'):
        html = re.sub(r'<button\b[^>]*data-' + attribute + r'="codex"[^>]*>.*?</button>',
                      '', html, flags=re.S)
    html = re.sub(r'<path\b[^>]*data-signal-route="codex"[^>]*/>', '', html)
    html = re.sub(r'<path\b[^>]*id="netD"[^>]*>.*?</path>', '', html, flags=re.S)
    html = re.sub(r'<div class="network-label account-d"[^>]*>.*?</div>', '', html, flags=re.S)
    html = html.replace('<span><b>CODEX</b></span><span class="sep" aria-hidden="true">·</span>', '')
    html = html.replace('<span>Codex</span>', '')
    for old, new in (
        ('Codex、WorkBuddy、Qoder 与 Trae', 'WorkBuddy、Qoder 与 Trae'),
        ('Codex、WorkBuddy、Qoder、Trae', 'WorkBuddy、Qoder、Trae'),
        ('Codex · WorkBuddy · Qoder · Trae', 'WorkBuddy · Qoder · Trae'),
        ('Codex 开发任务动画', '开发任务动画'),
        ('四种', '三种'), ('四入口', '三平台'), ('四网关', '三平台'),
        ('FOUR ENTRIES', 'THREE PLATFORMS'),
    ):
        html = html.replace(old, new)
    return html
