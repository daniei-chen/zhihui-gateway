// Local review only: native console fixtures, never a proxy to production.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {simplifyHome} from './simplify-home.mjs';

const site = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.join(site, 'home-source/public');
const consoleOrigin = 'http://127.0.0.1:4189';
const types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp',
  '.glb': 'model/gltf-binary', '.wasm': 'application/wasm', '.ico': 'image/x-icon',
  '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.txt': 'text/plain'};

export function reviewHtml(source) {
  const frame = (route, title) => `<iframe class="review-console-frame" src="${consoleOrigin}/${route}/" title="${title} · 本地合成数据" loading="lazy"></iframe>`;
  let html = source.replace(/<picture class="console-native-preview" data-preview-panel="([a-z-]+)">[\s\S]*?<\/picture>/g,
    (_, route) => frame(route, '控制台原生渲染'));
  html = html.replace(/(<div class="console-capture"[^>]*>)\s*<picture>[\s\S]*?<\/picture>/,
    (_, open) => open + frame('dashboard', '仪表盘原生渲染'));
  html = html.replace(/href="\/dashboard\/"/g, `href="${consoleOrigin}/dashboard/"`);
  html = html.replace('title="鹈鹕骑行：蓝色自行车、飘动围巾与海岸风景"', 'title="可旋转三维场景：海绵宝宝骑行与比奇堡街区"');
  html = html.replace('海风里的鹈鹕', '比奇堡的一天 · 拖动旋转');
  html = html.replace('03 / KEEP YOUR FLOW', '03 / A LITTLE JOY, A LITTLE AI');
  html = html.replace('复杂的留给智汇，<br>专注的留给你。', '带着好奇出发，<br>把快乐做出来。');
  html = html.replace('就像顺着海风骑行。<br>方向清楚，每一步都更从容。', '像海绵宝宝一样，<br>在 AI 的世界，创造一点快乐。');
  html = html.replace('把分散的工具与配置收在一处，从选择入口到查看用量，少一点来回切换，多一点连续的思考。', '让工具接住灵感，让想法变成作品。认真做事，也别忘了享受创造。');
  html = html.replace('开发任务动画：代码生成、测试进度与示例吞吐图表', '智汇工作空间：上下文汇聚、实现、验证与回执的动态示例');
  html = html.replace('从一句需求，到代码、文档和测试。<br>让熟悉的工具，各展所长。', '需求、实现、验证。<br>每一步，都有上下文。');
  html = html.replace('WorkBuddy、Qoder、Trae，连接不同的工作方式。你专注当前任务，智汇整理入口、授权与调用记录。', 'WorkBuddy、Qoder、Trae，各展所长。智汇整理入口，你专注眼前。');
  html = html.replace('Gateway Core<small>权限校验 · 请求记录</small>', '请求回执<small>用量依据 · 记录留存</small>');
  html = html.replace(/(<iframe id="pelicanRide"[^>]+)tabindex="-1"/, '$1tabindex="0"');
  html = html.replace('<iframe id="pelicanRide"', '<iframe loading="lazy" id="pelicanRide"');
  html = html.replace('</body>', '<script type="module" src="/review-paths.js"></script></body>');
  html = html.replace('</head>', `<link rel="stylesheet" href="/review-palette.css"><style>
    .review-console-frame{display:block;width:100%;height:min(72vw,770px);border:0;background:#fff}
    @media(max-width:760px){.review-console-frame{height:650px}}
    .review-badge{position:fixed;z-index:100;bottom:12px;right:12px;padding:7px 12px;border:1px solid #e7e8ec;border-radius:20px;background:#fffffff2;color:#687285;font:10px sans-serif;pointer-events:none}
    </style></head>`).replace('</body>', '<div class="review-badge">本地待审 · 合成数据 · 未上传</div></body>');
  return simplifyHome(html);
}

export function createReviewServer() {
  return http.createServer((request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    if (!['GET', 'HEAD'].includes(request.method)) {response.writeHead(405); return response.end();}
    let pathname;
    try {pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);}
    catch {response.writeHead(400); return response.end();}
    if (pathname === '/dashboard/' || pathname === '/accounts/') {
      response.writeHead(302, {Location: consoleOrigin + pathname}); return response.end();
    }
    if (pathname.startsWith('/api/') || pathname.includes('\\') || pathname.includes('\0') || pathname.startsWith('/login')) {
      response.writeHead(403); return response.end('Local visual review, no login or production APIs');
    }
    if (pathname === '/review-palette.css') {
      response.writeHead(200, {'Content-Type': types['.css']});
      return response.end(request.method === 'HEAD' ? '' : fs.readFileSync(path.join(site, 'home-source/scenes/color-review.css')));
    }
    if (pathname === '/review-paths.js') {
      response.writeHead(200, {'Content-Type': types['.js']});
      return response.end(request.method === 'HEAD' ? '' : fs.readFileSync(path.join(site, 'home-source/scenes/connected-paths.js')));
    }
    const file = path.resolve(root, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(root + path.sep)) {response.writeHead(403); return response.end();}
    fs.stat(file, (error, stat) => {
      if (error || !stat.isFile()) {response.writeHead(404); return response.end();}
      response.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
      response.setHeader('Content-Security-Policy', `default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; frame-src 'self' ${consoleOrigin}; object-src 'none'; base-uri 'self'; form-action 'none'`);
      response.writeHead(200);
      if (request.method === 'HEAD') return response.end();
      if (pathname === '/') return response.end(reviewHtml(fs.readFileSync(file, 'utf8')));
      fs.createReadStream(file).pipe(response);
    });
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  createReviewServer().listen(4188, '127.0.0.1', () => console.log('Local review: http://127.0.0.1:4188/'));
}
