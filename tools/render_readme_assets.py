"""Build self-contained README artwork; no network, fonts or raster tools needed."""
from __future__ import annotations

import argparse
import hashlib
import json
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/images/readme'
INK = '#202c42'
BLUE = '#627ff5'
MUTED = '#62738e'
LINE = '#dce6fc'
FONT = "'Bahnschrift', 'DIN Alternate', 'Microsoft YaHei', 'PingFang SC', sans-serif"
VERSION = (ROOT / 'VERSION').read_text(encoding='ascii').strip()


def text(x, y, value, size=18, color=INK, weight=400, **attrs):
    extra = ' '.join(f'{key.replace("_", "-")}="{escape(str(val), quote=True)}"'
                     for key, val in attrs.items())
    return (f'<text x="{x}" y="{y}" fill="{color}" font-family="{FONT}" '
            f'font-size="{size}" font-weight="{weight}" {extra}>{escape(value)}</text>')


def rect(x, y, w, h, r=20, fill='#ffffff', stroke=LINE):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}"/>'


def path(d, color=BLUE, width=2, **attrs):
    extra = ' '.join(f'{key.replace("_", "-")}="{escape(str(val), quote=True)}"'
                     for key, val in attrs.items())
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round" {extra}/>')


def zmark(x, y, scale=1):
    return (f'<g transform="translate({x} {y}) scale({scale})">'
            + path('M0 0 H40 L0 33 H40', 'url(#blue)', 8) + '</g>')


def dot(x, y, r=3, color=BLUE):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>'


def travel(d, begin='0s', duration='9s'):
    return (f'<circle class="travel" r="3" fill="{BLUE}" opacity=".8">'
            f'<animateMotion dur="{duration}" begin="{begin}" repeatCount="indefinite" path="{d}"/>'
            '</circle>')


def canvas(w, h, title, desc, body):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>
<defs>
  <linearGradient id="paper" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#ffffff"/><stop offset="1" stop-color="#f2f7ff"/></linearGradient>
  <linearGradient id="blue" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#9ebeff"/><stop offset=".58" stop-color="#6c8df6"/><stop offset="1" stop-color="#5472eb"/></linearGradient>
  <radialGradient id="halo"><stop stop-color="#dceaff" stop-opacity=".95"/><stop offset="1" stop-color="#eaf3ff" stop-opacity="0"/></radialGradient>
</defs>
<style>@media(prefers-reduced-motion:reduce){{.travel{{display:none}}}}</style>
{body}
</svg>
'''


def brand(x, y):
    return (zmark(x, y - 19, .57) + text(x + 40, y + 1, '智汇网关', 22, weight=700)
            + text(x + 41, y + 20, 'Z H I H U I   G A T E W A Y', 9, MUTED))


def chip(x, y, name, w):
    return rect(x, y, w, 36, 18) + dot(x + 17, y + 18, 3) + text(x + 29, y + 23, name, 13)


def hero(mobile=False):
    w, h = (640, 690) if mobile else (1280, 500)
    body = rect(1, 1, w - 2, h - 2, 30, 'url(#paper)')
    body += brand(42 if mobile else 54, 57 if mobile else 59)
    if not mobile:
        body += rect(1053, 39, 175, 33, 16, '#ffffff')
        body += text(1140, 60, f'SELF-HOSTED / {VERSION}', 11, MUTED, text_anchor='middle', letter_spacing='.6')
    x, top = (42, 136) if mobile else (59, 168)
    body += text(x, top, 'PRIVATE AI INFRASTRUCTURE', 11 if mobile else 12, BLUE, letter_spacing='2')
    body += text(x - 2, top + 66, '多源汇入，', 48 if mobile else 52, weight=700)
    body += text(x - 2, top + 129, '一核择路。', 48 if mobile else 52, weight=700)
    body += text(x, top + 174, '让每次调用，都走对路径。', 20 if mobile else 21, MUTED)
    cx, cy, radius = (336, 492, 164) if mobile else (926, 259, 220)
    body += f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="url(#halo)"/>'
    for r in ([105, 154] if mobile else [136, 194]):
        body += f'<ellipse cx="{cx}" cy="{cy}" rx="{r}" ry="{r * .63}" fill="none" stroke="{LINE}" opacity=".75"/>'
    zx, zy, scale = (272, 437, 3.1) if mobile else (846, 186, 4)
    body += f'<g opacity=".52">{zmark(zx - 5, zy + 9, scale)}</g>'
    body += zmark(zx, zy, scale)
    points = [(62, 412, 'WorkBuddy', 137), (438, 409, 'Qoder', 110), (445, 562, 'Trae', 98)] if mobile else [
        (696, 118, 'WorkBuddy', 137), (1070, 163, 'Qoder', 110), (1038, 357, 'Trae', 98)]
    flows = ['M199 430 C236 430 244 445 272 448', 'M438 427 C416 427 401 435 390 448', 'M445 580 C408 580 401 553 382 540'] if mobile else [
        'M833 136 C860 136 861 166 884 186', 'M1070 181 C1040 181 1034 194 1006 213', 'M1038 375 C1004 375 986 345 974 318']
    for i, (px, py, label, pw) in enumerate(points):
        body += path(flows[i], '#bed2ff', 1.4) + travel(flows[i], f'-{i * 3}s') + chip(px, py, label, pw)
    bottom = 636 if mobile else 439
    body += path(f'M{x} {bottom} H{w - x}', LINE, 1)
    body += text(x, bottom + 28, '三平台接入   /   独立授权   /   用量有据', 13, MUTED)
    if not mobile:
        body += text(w - x, bottom + 28, 'LESS NOISE. MORE CONTROL.', 10, MUTED, text_anchor='end', letter_spacing='1.5')
    return canvas(w, h, '智汇网关 · 多源汇入，一核择路',
                  '蓝白品牌封面。WorkBuddy、Qoder、Trae 汇入同一枚蓝色 Z。让每次调用，都走对路径。', body)


def capability_icon(kind, x, y):
    if kind == 0:
        paths = ['M0 3 H21 Q29 3 36 14 L53 36', 'M0 36 H53', 'M0 69 H21 Q29 69 36 58 L53 36']
        return (f'<g transform="translate({x} {y})">' + ''.join(path(d, '#a9c0fb') for d in paths)
                + rect(55, 18, 38, 36, 12, '#edf3ff') + zmark(65, 29, .4)
                + ''.join(dot(0, v, 3) for v in [3, 36, 69]) + '</g>')
    if kind == 1:
        return (f'<g transform="translate({x + 24} {y - 2})">'
                + path('M17 4 L44 14 V35 Q44 57 17 70 Q-10 57 -10 35 V14 Z', '#a9c0fb', 2)
                + path('M5 35 L14 44 L30 26', BLUE, 2.6) + '</g>')
    return (f'<g transform="translate({x + 4} {y - 1})">'
            + rect(0, 2, 77, 73, 14, '#f5f8ff')
            + path('M16 23 H58 M16 38 H41 M16 53 H48', '#9eb7f4', 2)
            + dot(59, 53, 4, '#5aaec0') + '</g>')


def capabilities(mobile=False):
    w, h = (640, 688) if mobile else (1280, 284)
    items = [('CONNECT', '一处接入', '三平台，独立路由。', '常用协议，共用访问边界。'),
             ('AUTHORIZE', '边界清楚', '凭证、模型与 IP 策略。', '按需授权，不混淆身份。'),
             ('OBSERVE', '消耗有据', '缓存命中与输出速度。', '用量缺失，不冒充零消耗。')]
    body = ''
    for i, (eyebrow, title, line1, line2) in enumerate(items):
        x, y, cw, ch = (1, 1 + i * 232, 638, 222) if mobile else (1 + i * 432, 1, 414, 282)
        body += rect(x, y, cw, ch, 24, 'url(#paper)')
        ix, iy = (x + 47, y + 65) if mobile else (x + 33, y + 47)
        body += capability_icon(i, ix, iy)
        tx, ty = (x + 182, y + 50) if mobile else (x + 30, y + 155)
        body += text(tx, ty, f'0{i + 1} / {eyebrow}', 10, BLUE, letter_spacing='1.7')
        body += text(tx, ty + 37, title, 27 if mobile else 24, weight=700)
        body += text(tx, ty + 70, line1, 17, MUTED)
        body += text(tx, ty + 97, line2, 15, MUTED)
    return canvas(w, h, '接入、授权、核对', '一处接入三平台，保持独立路由与权限；展示缓存命中和输出速度，不把缺失用量当作零。', body)


def routing(mobile=False):
    w, h = (640, 794) if mobile else (1280, 384)
    body = rect(1, 1, w - 2, h - 2, 26, 'url(#paper)')
    if mobile:
        body += text(34, 40, 'ONE ENTRY. DISTINCT ROUTES.', 11, BLUE, letter_spacing='1.6')
        body += rect(171, 71, 296, 69, 20) + text(319, 99, '你的客户端', 20, weight=700, text_anchor='middle')
        body += text(319, 122, '授权 Key · 模型 · 协议', 13, MUTED, text_anchor='middle')
        body += path('M319 140 V190', '#b8ccfb') + travel('M319 140 V190', '-2s')
        body += rect(171, 190, 296, 103, 24, '#edf3ff') + zmark(202, 225, .75)
        body += text(254, 233, '智汇网关', 23, weight=700) + text(254, 259, '鉴权 / 路由 / 计量', 14, MUTED)
        body += path('M319 293 V323 H75 V623', '#c9d8fa', 1.6)
        rows = [(347, 'WorkBuddy', '/v1/*'), (457, 'Qoder', '/qoder/v1/*'), (567, 'Trae', '/trae/v1/*')]
        for i, (y, name, prefix) in enumerate(rows):
            d = f'M75 {y + 44} H136'
            body += path(d, '#b7ccfc', 1.6) + travel(d, f'-{i * 2}s') + dot(75, y + 44, 3)
            body += rect(136, y, 402, 88, 20)
            body += text(161, y + 33, name, 22, weight=600) + text(161, y + 61, prefix, 17, MUTED)
        body += path('M34 700 H606', LINE, 1)
        body += text(34, 734, '管理视图 ≠ 客户端地址', 18, weight=600)
        body += text(34, 762, '协议能力以对应引擎及账号授权为准。', 14, MUTED)
    else:
        body += text(38, 43, 'ONE ENTRY. DISTINCT ROUTES.', 11, BLUE, letter_spacing='1.8')
        body += rect(42, 145, 239, 103, 22) + text(161, 186, '你的客户端', 23, weight=700, text_anchor='middle')
        body += text(161, 217, '授权 Key · 模型 · 协议', 14, MUTED, text_anchor='middle')
        body += path('M281 197 H432', '#adc5fb', 1.7) + travel('M281 197 H432', '-3s')
        body += rect(432, 129, 303, 137, 26, '#edf3ff') + zmark(466, 174, .9)
        body += text(526, 186, '智汇网关', 27, weight=700) + text(526, 219, '鉴权 / 路由 / 计量', 15, MUTED)
        for i, (y, name, prefix) in enumerate([(83, 'WorkBuddy', '/v1/*'), (165, 'Qoder', '/qoder/v1/*'), (247, 'Trae', '/trae/v1/*')]):
            d = f'M735 197 C795 197 797 {y + 30} 861 {y + 30}'
            body += path(d, '#bfd0f7', 1.7) + travel(d, f'-{i * 2}s')
            body += rect(861, y, 374, 60, 18) + dot(884, y + 30, 3)
            body += text(901, y + 26, name, 16, weight=600) + text(1214, y + 39, prefix, 15, MUTED, text_anchor='end')
        body += path('M38 332 H1242', LINE, 1)
        body += text(38, 360, '管理视图 ≠ 客户端地址', 13, MUTED)
        body += text(1242, 360, '协议能力以对应引擎及账号授权为准。', 12, MUTED, text_anchor='end')
    return canvas(w, h, '客户端、智汇与三平台路由',
                  '你的客户端经智汇鉴权、路由及计量后访问平台。WorkBuddy 使用 /v1/*，Qoder 使用 /qoder/v1/*，Trae 使用 /trae/v1/*。切换管理视图不会更改客户端地址。', body)


def button(label, width, primary=False):
    body = rect(1, 1, width - 2, 42, 15, INK if primary else '#f6f9ff', INK if primary else LINE)
    body += text(width / 2, 27, label, 14, '#ffffff' if primary else '#415678', 600, text_anchor='middle')
    return canvas(width, 44, label, label, body)


def assets():
    return {
        'cover.svg': hero(), 'cover-mobile.svg': hero(True),
        'capabilities.svg': capabilities(), 'capabilities-mobile.svg': capabilities(True),
        'routing.svg': routing(), 'routing-mobile.svg': routing(True),
        'button-home.svg': button('走进智汇 ↗', 144, True),
        'button-release.svg': button(f'{VERSION} 发布说明', 166),
        'button-develop.svg': button('开发与部署 ↓', 154),
    }


def public_manifest():
    preview = json.loads((ROOT / 'site/home/preview/manifest.json').read_bytes())
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    import re
    paths = sorted(set(re.findall(r'(?:src|srcset)="([^"]+)"', readme)))
    records = []
    for name in paths:
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError('README image missing or escaping the repository: ' + name)
        records.append({'path': name, 'bytes': path.stat().st_size,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return (json.dumps({'schema': 1, 'productionDataIncluded': False,
                        'consoleBuildId': preview['buildId'],
                        'scope': 'README artwork and historical synthetic console preview, not current production proof',
                        'assets': records}, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if committed SVGs differ')
    args = parser.parse_args()
    if not args.check:
        OUT.mkdir(parents=True, exist_ok=True)
    for name, content in assets().items():
        target = OUT / name
        if args.check:
            if not target.is_file() or target.read_bytes() != content.encode('utf-8'):
                raise SystemExit(f'Artwork needs regeneration: {target.relative_to(ROOT)}')
        else:
            target.write_text(content, encoding='utf-8', newline='\n')
    target = OUT / 'manifest.json'
    manifest = public_manifest()
    if args.check:
        if not target.is_file() or target.read_bytes() != manifest:
            raise SystemExit('README image provenance needs regeneration')
    else:
        target.write_bytes(manifest)
    print(f'{len(assets())} README assets {"verified" if args.check else "written"}')


if __name__ == '__main__':
    main()
