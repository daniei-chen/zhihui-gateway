"""Build the local light refinement from the unchanged read-only snapshot.

No network access. Capture provenance in source-snapshot is never rewritten.
"""
from pathlib import Path
import json
import re
import shutil
import os
import subprocess
from build_scenes import build_task_scene
from page_content import revise_page

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source-snapshot" / "site"
PUBLIC = ROOT / "public"


def div_block(document, marker):
    start = document.index(marker)
    depth = 0
    for match in re.finditer(r"</?div\b[^>]*>", document[start:]):
        depth += -1 if match.group().startswith("</") else 1
        if depth == 0:
            return document[start:start + match.end()]
    raise ValueError("Unbalanced div: " + marker)


PUBLIC.mkdir(exist_ok=True)
(PUBLIC / "styles").mkdir(exist_ok=True)
(PUBLIC / "scripts").mkdir(exist_ok=True)
for source in SOURCE.iterdir():
    if source.name in {"index.html", "BUILD_INFO.json", "CONTENT-SHA256.txt"}:
        continue
    target = PUBLIC / source.name
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)

html = (SOURCE / "index.html").read_text(encoding="utf-8")
styles = re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.S)
# The old page accumulated several independent animation patches. Only the new
# controller owns motion; retain the source layout without its animation rules.
base_css = re.sub(r"(?<![-\w])animation(?:-[\w]+)?\s*:[^;}]+;?", "", "\n\n".join(styles))
(PUBLIC / "styles" / "source.css").write_text(base_css, encoding="utf-8")
html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.S)
html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
html = html.replace("</head>", '<link rel="stylesheet" href="/styles/source.css"/>\n<link rel="stylesheet" href="/styles/upgrade.css"/>\n<link rel="stylesheet" href="/styles/motion.css"/>\n<link rel="stylesheet" href="/styles/chapters.css"/>\n<link rel="stylesheet" href="/styles/sculpture.css"/>\n</head>')
html = html.replace('<html lang="zh-CN">', '<html lang="zh-CN" class="zh-light-refinement">')
html = html.replace("<body>", '<body>\n<a class="skip-link" href="#product">跳到主要内容</a>')
for marker in ['<div class="intelligence-field"', '<div aria-hidden="true" class="journey-rail"', '<div class="pointer-aura"']:
    if marker in html:
        html = html.replace(div_block(html, marker), "", 1)
html = re.sub(r'<circle[^>]*id="movingSpark".*?</circle>', "", html, flags=re.S)
html = re.sub(r'<path\b[^>]*id="movingPath"[^>]*></path>', "", html)
html = html.replace('<div id="top">', '<div id="top" data-motion-scope>\n<div class="ambient-flow" aria-hidden="true"><canvas id="zh-intelligence-canvas"></canvas></div>')
html = html.replace('aria-pressed="false" hidden>暂停动效', 'aria-pressed="false">暂停动效')
html = html.replace('智汇管理控制台结构演示：左侧导航、调用和路由概览、账号池状态与请求追踪。所有状态为示意，未连接真实接口。', '智汇控制台预览：顶部工具栏、仪表盘和底部 Dock，使用合成示例数据。')
html = html.replace('交互示意 · 非实时数据。真实账号与用量，请进入控制台查看。', '真实控制台界面 · 示例数据。账号与用量请登录查看。')
html = html.replace('<span class="capture-label">控制台原生渲染 · 合成数据</span>', '')
html = html.replace('/favicon/zhihui-z-20261029/', '/')
html = html.replace('href="/dashboard/"', 'href="https://zhihui.kaogong.art/dashboard/"')
html = html.replace('>进入首页 ↗</a>', '>进入控制台 ↗</a>')
html = html.replace('>控制台演示</a>', '>工作台预览</a>')
html = html.replace('class="partners-marquee"', 'class="partners-marquee" data-motion-scope')

html = html.replace('</header>\n</div>', '</header>\n</div>\n' + (ROOT / 'sections/intro-screens.html').read_text(encoding='utf-8') + '\n' + (ROOT / 'sections/sculpture.html').read_text(encoding='utf-8'), 1)

# Only the public homepage's same-build synthetic fixture assets are copied.
panels = [
    ("overview", "仪表盘", "dashboard"),
    ("accounts", "账号", "accounts"),
    ("keys", "密钥", "keys"),
    ("models", "模型", "models"),
    ("usage", "用量", "stats"),
    ("logs", "日志", "logs"),
    ("tasks", "任务", "tasks"),
    ("security", "安全", "security"),
]
buttons, images = [], []
for index, (key, label, image) in enumerate(panels):
    selected = index == 0
    buttons.append(f'<button type="button" class="workspace-tab{" active" if selected else ""}" data-console="{key}" aria-pressed="{str(selected).lower()}" aria-controls="workspace-{key}">{label}</button>')
    images.append(f'''<div class="console-panel" data-panel="{key}" id="workspace-{key}"{" hidden" if not selected else ""}>
<picture class="console-native-preview" data-preview-panel="{image}">
<source media="(max-width:760px)" srcset="/preview/{image}-mobile.jpg" width="390" height="650"/>
<img src="/preview/{image}-desktop.jpg" width="1440" height="930" alt="智汇网关{label}实际构建界面，合成示例数据" loading="lazy" decoding="async"/>
</picture></div>''')
workspace = '''<section class="console-section" id="console" data-motion-scope><div class="container">
<div class="ext-header reveal"><div><div class="overline">04 / WORKSPACE</div><h2 class="big-title">掌控，在指尖。</h2></div><p class="section-lead">从全局概览，到每一次调用。</p></div>
<div class="native-workspace reveal">
<div class="workspace-tabs" role="group" aria-label="选择工作台预览页面">''' + "\n".join(buttons) + '''</div>
<div class="workspace-context" aria-live="polite"><div><h3 id="workspaceTitle">仪表盘</h3><p id="workspaceDescription">账号健康、调用趋势与积分收支，一眼有数。</p></div><span class="preview-note">界面预览 · 示例数据</span></div>
<div class="workspace-stage">''' + "\n".join(images) + '''</div>
<div class="workspace-foot"><p>顶部切换平台，底部 Dock 直达常用功能。</p><a href="https://zhihui.kaogong.art/dashboard/">进入控制台 <span aria-hidden="true">↗</span></a></div>
</div></div></section>'''
html = re.sub(r'<section class="console-section".*?</section>', lambda _: workspace, html, count=1, flags=re.S)
html = revise_page(html)
html = subprocess.run([os.environ.get('NODE_BINARY', 'node'), str(ROOT.parent / 'tools/simplify-home.mjs')],
                      input=html, text=True, encoding='utf-8', capture_output=True, check=True).stdout
html = html.replace("</body>", '<script src="/scripts/upgrade.js"></script>\n<script src="/scripts/motion.js"></script>\n<script src="/scripts/ride.js"></script>\n<script type="module" src="/scripts/sculpture.js"></script>\n</body>')
(PUBLIC / "index.html").write_text(html, encoding="utf-8")
manifest_path = PUBLIC / "site.webmanifest"
webmanifest = json.loads(manifest_path.read_text(encoding="utf-8"))
webmanifest["start_url"] = "/"
webmanifest["description"] = "智汇 · WorkBuddy / Qoder / Trae 三平台入口"
manifest_path.write_text(json.dumps(webmanifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Original scene source is the publication authority.
build_task_scene()
print(f"Prepared light refinement: {len(styles)} original style blocks; duplicate workspace removed.")
