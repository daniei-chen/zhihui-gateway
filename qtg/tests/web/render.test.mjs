/* 渲染冒烟：用极简 DOM shim 真实执行前端模块，验证不白屏、关键文本渲染出来。 */
import fs from 'node:fs';
import path from 'node:path';
import { installDom } from './dom-shim.mjs';

const FIX = process.env.QTG_FIXTURES || '/tmp/qtg-fixtures';
import { fileURLToPath } from 'node:url';
const APP_DIR = process.env.QTG_APP_DIR
  || fileURLToPath(new URL('../../qtg/web/assets/js', import.meta.url));

const { document, window } = installDom();

/* 1. 构造 index.html 的骨架节点 */
const ids = ['login', 'loginForm', 'loginPassword', 'loginError', 'shell', 'main',
  'pageTitle', 'pageSub', 'pageActions', 'page', 'dock', 'toasts', 'modalRoot',
  'brandSub', 'refreshBtn', 'themeBtn', 'logoutBtn'];
for (const id of ids) {
  const e = document.createElement(id.endsWith('Btn') || id.endsWith('Form') ? 'button' : 'div');
  e.id = id;
  document.documentElement.append(e);
}
document.getElementById('login').hidden = true;
document.getElementById('shell').hidden = false;

/* 2. mock fetch：按路径映射到真实采集的 fixture */
const map = {
  'api/v1/auth/session': { authenticated: true, expires_at: Date.now() / 1000 + 3600 },
  'api/v1/overview': JSON.parse(fs.readFileSync(path.join(FIX, 'overview.json'), 'utf8')),
  'api/v1/accounts': JSON.parse(fs.readFileSync(path.join(FIX, 'accounts.json'), 'utf8')),
  'api/v1/usage': JSON.parse(fs.readFileSync(path.join(FIX, 'usage.json'), 'utf8')),
  'api/v1/keys': JSON.parse(fs.readFileSync(path.join(FIX, 'keys.json'), 'utf8')),
  'api/v1/models': JSON.parse(fs.readFileSync(path.join(FIX, 'models.json'), 'utf8')),
  'api/v1/requests': JSON.parse(fs.readFileSync(path.join(FIX, 'requests.json'), 'utf8')),
  'api/v1/tasks': JSON.parse(fs.readFileSync(path.join(FIX, 'tasks.json'), 'utf8')),
  'api/v1/ops': JSON.parse(fs.readFileSync(path.join(FIX, 'ops.json'), 'utf8')),
  'api/v1/settings': JSON.parse(fs.readFileSync(path.join(FIX, 'settings.json'), 'utf8')),
  'api/v1/logs': JSON.parse(fs.readFileSync(path.join(FIX, 'logs.json'), 'utf8')),
  'api/v1/alerts': JSON.parse(fs.readFileSync(path.join(FIX, 'alerts.json'), 'utf8')),
  'api/v1/audit': JSON.parse(fs.readFileSync(path.join(FIX, 'audit.json'), 'utf8')),
  'api/v1/data-plane': { enabled: true, base_url: 'http://127.0.0.1:8795/v1', upstreams: { qoder: { configured: false, models: 0, key_file: '/x' }, trae: { configured: false, models: 0, key_file: '/y' } }, keys: 1, requests_total: 3 },
  'api/v1/qtg-keys': { keys: [{ id: 'abc', name: '测试', masked: 'sk-qtg-****abc', enabled: true, platforms: ['qoder'], models: [], daily_limit: 0, created_at: '', last_used_at: '' }] },
  'api/v1/proxy-stats': { window_days: 1, requests: 3, tokens: 450, errors: 0, by_platform: [], by_key: [], by_model: [] },
  'api/v1/proxy-requests': { requests: [] },
  'api/v1/system': { service: { version: '1.0.0', uptime_text: '0h1m', pid: 1, host: '127.0.0.1', port: 8795, proxy_enabled: true }, runtime: { python: '3.12.3', deps: { fastapi: '0.141.1' } }, storage: { data_dir: '/x', data_size: 1024, audit_records: 3, audit_size: 100, db_size: 200, audit_integrity: { ok: true } }, release: { present: true, signed: true, files: 63 }, limits: { upstream_timeout_sec: 20 } },
  'api/v1/security': { session: { via: 'cookie', ttl_sec: 28800 }, tokens: { primary: '/x', compatible: ['/y'], read_requires_auth: false, cookie_secure: 'auto' }, data_plane: { keys_total: 1 }, upstream_secrets: { qoder_configured: true, trae_configured: true, qoder_key_file: '/a', trae_key_file: '/b' }, audit: { ok: true }, hardening: { systemd: 'x' } },
};
const calls = [];
globalThis.fetch = async (url) => {
  const clean = String(url).replace(/^\.?\//, '').split('?')[0];
  calls.push(clean);
  const key = Object.keys(map).find((k) => clean.startsWith(k));
  const body = key ? map[key] : { error: { code: 'not_found', message: 'no fixture' } };
  return {
    ok: !!key, status: key ? 200 : 404,
    text: async () => JSON.stringify(body),
  };
};

/* 3. 执行前端入口 */
const errors = [];
window.addEventListener = () => {};
process.on('unhandledRejection', (e) => errors.push(e));
window.onerror = (e) => errors.push(e);

await import(path.join(APP_DIR, 'app.js'));
await new Promise((r) => setTimeout(r, 300));

/* 4. 断言 */
function textOf(id) {
  const el = document.getElementById(id);
  return el ? el.textContent : '';
}
function assert(cond, msg) {
  if (cond) { console.log('  ok  -', msg); return true; }
  console.error('  FAIL-', msg); errors.push(new Error(msg)); return false;
}

console.log('=== 渲染冒烟 ===');
assert(document.getElementById('login').hidden === true, '登录后隐藏登录页');
assert(document.getElementById('shell').hidden === false, '登录后显示主界面');
assert(textOf('pageTitle') === '概览', `页标题为「概览」（实际: ${textOf('pageTitle')}）`);
const page = document.getElementById('page');
assert(page.children.length > 0, `页面已渲染 ${page.children.length} 个区块`);
const dock = document.getElementById('dock');
const navBtns = dock.children.filter((c) => c.dataset && c.dataset.path);
assert(navBtns.length === 12, `dock 渲染 ${navBtns.length} 个导航项（期望 12）`);
assert(dock.children.length >= 11, `dock 含快速添加按钮（共 ${dock.children.length} 项）`);
assert(dock.children.some((c) => c.getAttribute('data-path') === 'overview'), 'dock 含 overview');
const pageText = page.textContent;
assert(pageText.includes('账号总数'), '概览含「账号总数」统计卡');
// 统计卡数值现在走 countUp 动画，断言 data-raw（稳定值）而不是动画中间态
const raws = [...page.querySelectorAll('.stat-value')].map((n) => Number(n.dataset.raw));
assert(raws.length === 5, `统计卡 ${raws.length} 张（期望 5）`);
assert(raws[3] === 2450, `积分余额 data-raw=${raws[3]}（期望 2450）`);
assert(raws[4] === 94651, `14 天 Token data-raw=${raws[4]}（期望 94651）`);
await new Promise((r) => setTimeout(r, 900));
const afterText = page.textContent;  // 重新取：countUp 完成后 DOM 才更新
assert(afterText.includes('2,450') && afterText.includes('94.7k'), 'countUp 结束后渲染真实数值');
assert(calls.some((c) => c.startsWith('api/v1/overview')), '调用了 overview 接口');

/* 5. 逐页渲染（通过 hash 路由切换） */
const pages = ['accounts', 'usage', 'keys', 'models', 'logs', 'alerts', 'tasks', 'settings', 'audit', 'playground', 'requests'];
for (const p of pages) {
  location.hash = '#/' + p;
  window.dispatchEvent?.({ type: 'hashchange' });
  // shim 不派发事件，直接走 router.render
  const router = await import(path.join(APP_DIR, 'router.js'));
  const cleanup = await router.render({ path: p, params: {} });
  if (typeof cleanup === 'function') cleanup();  // 清理页面的定时器，否则进程不退出
  const root = document.getElementById('page');
  const title = textOf('pageTitle');
  assert(root.children.length > 0, `页面 ${p} 渲染出内容（标题: ${title}）`);
}

console.log(`\n=== 结果: ${errors.length === 0 ? 'PASS' : 'FAIL(' + errors.length + ')'} ===`);
if (errors.length) { for (const e of errors) console.error(e.message || e); process.exit(1); }
