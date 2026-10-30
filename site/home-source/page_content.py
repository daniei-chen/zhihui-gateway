"""Editorial changes and the four-entry layout for the existing homepage."""
from pathlib import Path
import re
from release_scope import three_platforms

ROOT = Path(__file__).resolve().parent


def revise_page(html):
    replacements = {
        '统一管理 WorkBuddy、Qoder 与 Trae 账号池，提供兼容 API、访问凭证、调用记录及用量管理。': '连接 Codex、WorkBuddy、Qoder 与 Trae，整理工具入口、授权、调用记录与用量。',
        '多源汇入，一核择路。': '四种入口，同一个智汇。',
        '让每次调用，': '让灵感出发，',
        '都走对路径。': '让工作向前。',
        'WorkBuddy、Qoder、Trae，汇入一个清晰入口。': 'Codex、WorkBuddy、Qoder、Trae，汇入一个清晰入口。',
        '三平台接入': '四种入口',
        '三种来源，一个入口。': '四种工作方式，一个清晰入口。',
        '三种来源，各行其路。': '熟悉的工具，有序地连接。',
        '三平台蓝色光路，选择平台可同步接入示例': '四入口蓝色光路，选择工具可同步查看接入说明',
        '复杂的，留给网关。': '从接入到回看，都在这里。',
        '从接入到用量，恰好各就其位。': '入口、授权、用量，按工作的顺序安排。',
        '多源接入': '入口归一',
        '按需授权': '权限随需',
        '谁能用、用什么，由你定义。': '谁来用、能用什么，清楚地设定。',
        '用量有据': '用量可查',
        '调用可追溯，未知不归零。': '看得到消耗，也保留待核对的部分。',
        '选择来源，光路与代码随之切换。': '选择工具，查看对应的接入方式。',
        'SELECT A SOURCE': 'CHOOSE YOUR TOOL',
        '选择来源</h3>': '从你的习惯开始</h3>',
        '独立引擎，统一控制面。': 'Codex、WorkBuddy、Qoder、Trae，各有入口，各守边界。',
        '一条光路，看清去向。': '一次调用，路径清楚。',
        '切换场景，跟随一次请求。': '从入口校验到路径选择，看看请求如何向前。',
        '每一笔，都有下落。': '每次调用，留下一份依据。',
        '已知结算，未知留待核对。': '已确认的记清楚，未确认的留待核对。',
        '掌控，在指尖。': '打开工作台，接着往下做。',
        '从全局概览，到每一次调用。': '账号、密钥、模型与用量，回到同一个工作空间。',
        '沿用熟悉的接入。': '选好入口，就从这里开始。',
        '选好入口，复制你的配置。': '按工具查看配置，复制后替换为你的授权信息。',
        '替换域名、Key 与授权模型后使用。': '实际地址、协议与可用模型，以你的控制台配置为准。',
        '入口开放，边界清晰。': '连接更多，边界依然清楚。',
        '只向授权用户开放。': '在你的授权范围内，使用你的工具与资源。',
        '不开放注册，不公开售卖额度。': '仅向获授权用户开放，按权限使用。',
        '密钥留在控制台，示例只用占位符。': '凭据在控制台中管理，公开页面使用示例。',
        '不把示意图，当作实时业务数据。': '界面预览与运行状态分开呈现。',
        '你可能想知道。': '开始之前，了解这些。',
        '简短回答，清晰边界。': '工具、授权与数据，一次说清。',
        '三平台是否共用同一套上游账号？': '四种入口可以混用账号和权限吗？',
        '不混用。三平台有各自的入口、引擎与授权边界。': '请按各入口的授权与接入配置使用。Codex、WorkBuddy、Qoder、Trae 的配置和权限不应直接互换。',
        '所有模型在任何时候都可用吗？': '怎样确认当前能用的模型？',
        '以控制台为准。可用范围受平台、授权、额度与运行状态影响。': '在控制台查看当前模型范围。可用性会随工具、授权、额度和运行状态变化。',
        '上游没有返回 Token 用量怎么办？': '用量暂时缺失，会算成零吗？',
        '保留 unknown 等待核对，不把未知消耗算作零。': '不会。未知用量保留为 unknown，待核对后再处理。',
        '首页里的图表是实时数据吗？': '动画和工作台预览是实时运行画面吗？',
        '不是。首页图示不读取真实账号与调用；实时用量请进入控制台。': '开发任务动画展示工作过程，图表数字为演示值；工作台预览使用合成示例数据。真实账号与用量请登录查看。',
        '就从智汇开始。': '下一个想法，从智汇出发。',
        '一个入口，让掌控回归简单。': '四种入口，连接你的下一步。',
        '统一入口 · 独立边界': 'Codex · WorkBuddy · Qoder · Trae',
        '01 / PLATFORMS': '05 / FOUR ENTRIES',
        '02 / ROUTING': '06 / REQUEST PATH',
        '03 / LEDGER': '07 / USAGE RECORD',
        '04 / WORKSPACE': '08 / WORKSPACE',
        '05 / CONNECT': '09 / GET CONNECTED',
    }
    for old, new in replacements.items():
        html = html.replace(old, new)
    html = html.replace('href="#product">向下探索', 'href="#task-scene">向下探索')
    html = html.replace('<a href="#platforms">平台</a><a href="#routing">光路</a><a href="#console">工作台</a><a href="#setup">接入</a>', '<a href="#task-scene">体验</a><a href="#platforms">四种入口</a><a href="#console">工作台</a><a href="#setup">接入</a>')
    html = html.replace('<div class="mobile-menu" id="mobileMenu">', '<div class="mobile-menu" id="mobileMenu"><a href="#task-scene">开发任务演示</a><a href="#ride-preview">专注向前</a>')
    html = html.replace('<div class="source-tags"><span>WorkBuddy', '<div class="source-tags"><span>Codex</span><span>WorkBuddy')

    # Add Codex across every selection surface without inventing a production route.
    selectors = [
        ('<button aria-pressed="true" class="platform-choice active"', '<button aria-pressed="false" class="platform-choice" data-platform="codex" type="button"><span><span class="choice-name">Codex</span><span class="choice-desc">开发任务入口</span></span><span class="choice-arrow">↗</span></button>'),
        ('<button class="signal-source is-selected"', '<button class="signal-source" type="button" data-hero-platform="codex" aria-pressed="false"><span class="signal-source-mark">C</span><span><strong>Codex</strong><small>开发任务</small></span><span class="signal-source-arrow" aria-hidden="true">↗</span></button>'),
        ('<button aria-pressed="true" class="mode selected"', '<button aria-pressed="false" class="mode" data-route="codex" type="button">Codex</button>'),
        ('<button aria-pressed="true" class="code-tab on"', '<button aria-pressed="false" class="code-tab" data-code="codex" type="button">Codex</button>'),
    ]
    for marker, addition in selectors:
        html = html.replace(marker, addition + marker, 1)
    signal_paths = '\n'.join(f'<path data-signal-route="{key}" class="{"is-selected" if key == "workbuddy" else ""}" d="M260 180 C325 180 330 {y} 391 {y}"/>' for key, y in [('codex',45),('workbuddy',135),('qoder',225),('trae',315)])
    html = re.sub(r'<path data-signal-route="workbuddy".*?<path data-signal-route="trae"[^>]*/>', lambda _: signal_paths, html, count=1, flags=re.S)
    for identity, y in [('netA',135),('netC',220),('netB',305)]:
        html = re.sub(r'<path class="lines[^\"]*" d="[^"]+" id="' + identity + r'"></path>', f'<path class="lines" d="M80 90 C160 90 183 178 271 178 C363 178 370 {y} 472 {y}" id="{identity}"></path>', html)
    html = html.replace('<path class="lines"', '<path class="lines dim" d="M80 90 C160 90 183 178 271 178 C363 178 370 50 472 50" id="netD"></path><path class="lines"', 1)
    html = html.replace('<div class="network-label account-a', '<div class="network-label account-d" data-platform-target="codex"><i class="state-light"></i>Codex<small>按控制台配置</small></div><div class="network-label account-a', 1)
    html = html.replace('DATA PLANE PREFIX', 'ACCESS / 入口').replace('ENGINE / ADAPTER', 'SERVICE / 适配')
    html = html.replace('<p class="hint">', '<p class="hint" id="setupHint">', 1)

    entries = ''.join(f'<span><b>{name}</b></span><span class="sep" aria-hidden="true">·</span>' for name in ['CODEX', 'WORKBUDDY', 'QODER', 'TRAE'])
    html = re.sub(r'<div aria-label="项目支持的上游平台和主要功能".*?</div></div>', '<div aria-label="Codex、WorkBuddy、Qoder、Trae 四种入口" class="partners-marquee" data-motion-scope><div aria-hidden="true" class="partner-track">' + entries * 2 + '</div></div>', html, count=1, flags=re.S)
    html = re.sub(r'<section class="section how".*?</section>', '', html, count=1, flags=re.S)
    html = re.sub(r'<section class="arch-section".*?</section>', '', html, count=1, flags=re.S)
    html = html.replace('<section class="section code-section"', (ROOT / 'sections/details.html').read_text(encoding='utf-8') + '\n<section class="section code-section"', 1)
    return three_platforms(html)
