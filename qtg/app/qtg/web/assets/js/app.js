/* 应用入口：登录、导航、主题、命令面板、全局错误边界。 */
import { api, ApiError } from './api.js';
import * as router from './router.js';
import { $, el, ICONS, modal, toast, confirmDialog, attachDockMagnify } from './ui.js';

import overviewPage from './pages/overview.js';
import accountsPage from './pages/accounts.js';
import usagePage from './pages/usage.js';
import keysPage from './pages/keys.js';
import modelsPage from './pages/models.js';
import logsPage from './pages/logs.js';
import alertsPage from './pages/alerts.js';
import tasksPage from './pages/tasks.js';
import settingsPage from './pages/settings.js';
import auditPage from './pages/audit.js';
import playgroundPage from './pages/playground.js';
import requestsPage from './pages/requests.js';

const NAV = [
  { path: 'overview', label: '概览', icon: 'dash', key: 'g o' },
  { path: 'accounts', label: '账号', icon: 'users', key: 'g a' },
  { path: 'usage', label: '用量', icon: 'usage', key: 'g u' },
  { path: 'keys', label: '密钥', icon: 'key', key: 'g k' },
  { path: 'models', label: '模型', icon: 'model', key: 'g m' },
  { path: 'logs', label: '日志', icon: 'logs', key: 'g l' },
  { path: 'alerts', label: '告警', icon: 'alert', key: 'g e' },
  { path: 'tasks', label: '任务', icon: 'checkin', key: 'g t' },
  { path: 'settings', label: '设置', icon: 'settings', key: 'g s' },
  { path: 'audit', label: '审计', icon: 'audit' },
  { path: 'playground', label: '试跑', icon: 'checkin', key: 'g p' },
  { path: 'requests', label: '流水', icon: 'usage', key: 'g r' },
];

const PAGES = {
  overview: overviewPage, accounts: accountsPage, usage: usagePage, keys: keysPage,
  models: modelsPage, logs: logsPage, alerts: alertsPage, tasks: tasksPage,
  settings: settingsPage, audit: auditPage,
  playground: playgroundPage, requests: requestsPage,
};

/* ---------------- 主题 ---------------- */
const THEME_KEY = 'qtg-theme';
function applyTheme(mode) {
  const dark = mode === 'dark' || (mode !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  const html = document.documentElement;
  html.classList.add('theme-switching');       // 颜色平滑过渡，避免切主题闪白
  html.classList.toggle('dark', dark);
  html.classList.toggle('light', !dark);
  localStorage.setItem(THEME_KEY, mode);
  setTimeout(() => html.classList.remove('theme-switching'), 260);
}
function initTheme() {
  applyTheme(localStorage.getItem(THEME_KEY) || 'system');
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if ((localStorage.getItem(THEME_KEY) || 'system') === 'system') applyTheme('system');
  });
}

/* ---------------- Dock ---------------- */
function renderDock() {
  const dock = $('#dock');
  dock.replaceChildren();
  for (const item of NAV) {
    dock.append(el('button', {
      class: 'dock-btn', type: 'button', title: `${item.label}${item.key ? ' (' + item.key.toUpperCase() + ')' : ''}`,
      'data-path': item.path,
      'aria-current': item.path === (router.currentRoute() || 'overview') ? 'page' : null,
      onclick: () => router.navigate(item.path),
    },
      el('span', { html: ICONS[item.icon], 'aria-hidden': 'true' }),
      el('span', { text: item.label }),
    ));
  }
  dock.append(el('div', { class: 'dock-sep' }));
  dock.append(el('button', {
    class: 'dock-btn', type: 'button', title: '快速添加账号', 'aria-label': '快速添加账号',
    onclick: () => openQuickAdd(),
  },
    el('span', { html: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>' }),
    el('span', { text: '添加' }),
  ));
}
function syncDockActive(path) {
  document.querySelectorAll('#dock .dock-btn').forEach((btn) => {
    if (btn.dataset.path) btn.setAttribute('aria-current', btn.dataset.path === path ? 'page' : 'false');
  });
}

/* ---------------- 命令面板（Ctrl+K） ---------------- */
let paletteOpen = false;
function openQuickAdd() {
  router.navigate('accounts');
  setTimeout(() => window.dispatchEvent(new CustomEvent('qtg:quick-add')), 400);
}
function commands() {
  const list = NAV.map((n) => ({
    label: `跳转到 ${n.label}`, hint: n.key ? n.key.toUpperCase() : '', icon: n.icon,
    run: () => router.navigate(n.path),
  }));
  list.push(
    { label: '一键签到（Qoder + Trae）', hint: '动作', icon: 'checkin', run: async () => {
        const r = await api.checkin('all');
        const q = r.data?.qoder?.results || [];
        toast('签到完成', `Qoder ${q.filter((x) => x.ok).length}/${q.length} 成功`, 'ok');
      } },
    { label: '立即备份 Qoder', hint: '动作', icon: 'audit', run: async () => { await api.backup('qoder'); toast('备份完成', 'Qoder', 'ok'); } },
    { label: '立即备份 Trae', hint: '动作', icon: 'audit', run: async () => { await api.backup('trae'); toast('备份完成', 'Trae', 'ok'); } },
    { label: '清除缓存并刷新', hint: '动作', icon: 'refresh', run: refreshAll },
    { label: '切换主题', hint: '动作', icon: 'settings', run: toggleTheme },
    { label: '退出登录', hint: '动作', icon: 'account', run: doLogout },
  );
  return list;
}
function toggleTheme() {
  const cur = localStorage.getItem(THEME_KEY) || 'system';
  const next = cur === 'dark' ? 'light' : cur === 'light' ? 'system' : 'dark';
  applyTheme(next);
  toast('主题', next === 'system' ? '跟随系统' : next === 'dark' ? '深色' : '浅色', 'ok', 1200);
}
function openPalette() {
  if (paletteOpen) return;
  paletteOpen = true;
  const all = commands();
  let filtered = all;
  let active = 0;

  const input = el('input', { type: 'search', placeholder: '搜索页面或动作…（↑↓ 选择，Enter 执行）', 'aria-label': '命令面板' });
  const listBox = el('div', { class: 'list', style: 'max-height:340px;overflow:auto;margin-top:10px' });
  const mask = el('div', { class: 'modal-mask', role: 'presentation' });
  const box = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': '命令面板', style: 'width:min(560px,100%)' },
    input, listBox);
  mask.append(box);

  const close = () => { paletteOpen = false; mask.remove(); document.removeEventListener('keydown', onKey, true); };
  const paint = () => {
    listBox.replaceChildren(...(filtered.length ? filtered.map((c, i) => el('button', {
      class: 'list-item', type: 'button',
      style: `width:100%;text-align:left;border:0;cursor:pointer;${i === active ? 'background:var(--muted)' : ''}`,
      onclick: async () => { close(); try { await c.run(); } catch (e) { toast('执行失败', e.message, 'bad'); } },
    },
      el('span', { class: 'dot', style: 'background:var(--chart-1)' }),
      el('span', { class: 'grow', html: ICONS[c.icon] || '' }),
      el('span', { class: 'grow ellipsis', style: 'font-size:13px', text: c.label }),
      c.hint ? el('span', { class: 'xs muted', text: c.hint }) : null,
    )) : [el('div', { class: 'empty', text: '没有匹配项' })]));
  };
  const onKey = (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); close(); }
    else if (ev.key === 'ArrowDown') { ev.preventDefault(); active = Math.min(active + 1, filtered.length - 1); paint(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); active = Math.max(active - 1, 0); paint(); }
    else if (ev.key === 'Enter') {
      ev.preventDefault();
      const c = filtered[active];
      if (c) { close(); Promise.resolve(c.run()).catch((e) => toast('执行失败', e.message, 'bad')); }
    }
  };
  input.addEventListener('input', () => {
    const kw = input.value.trim().toLowerCase();
    filtered = kw ? all.filter((c) => c.label.toLowerCase().includes(kw) || (c.hint || '').toLowerCase().includes(kw)) : all;
    active = 0;
    paint();
  });
  mask.addEventListener('mousedown', (e) => { if (e.target === mask) close(); });
  document.addEventListener('keydown', onKey, true);
  $('#modalRoot').append(mask);
  paint();
  setTimeout(() => input.focus(), 20);
}

/* ---------------- 登录 ---------------- */
function showLogin(message = '') {
  const shell = $('#shell');
  const login = $('#login');
  shell.hidden = true;
  shell.style.display = 'none';
  login.hidden = false;
  login.style.display = 'grid';
  const err = $('#loginError');
  if (err) err.textContent = message;
  setTimeout(() => $('#loginPassword')?.focus(), 50);
}
function showShell() {
  const login = $('#login');
  const shell = $('#shell');
  login.hidden = true;
  login.style.display = 'none';
  shell.hidden = false;
  shell.style.display = 'flex';
  renderDock();
  attachDockMagnify($('#dock'));
}

async function doLogin(ev) {
  ev.preventDefault();
  const input = $('#loginPassword');
  const btn = $('#loginForm button[type=submit]');
  const err = $('#loginError');
  err.textContent = '';
  btn.disabled = true;
  btn.textContent = '登录中…';
  try {
    await api.login(input.value.trim());
    input.value = '';
    showShell();
    await bootPages();
    toast('已登录', 'Ctrl+K 打开命令面板', 'ok');
  } catch (e) {
    err.textContent = e.message || '登录失败';
  } finally {
    btn.disabled = false;
    btn.textContent = '进入控制台';
  }
}

async function doLogout() {
  try { await api.logout(); } catch { /* 忽略 */ }
  pagesStarted = false;
  location.hash = '';
  showLogin();
  toast('已退出', '', 'ok');
}

/* ---------------- 页面启动 ---------------- */
let pagesStarted = false;
async function bootPages() {
  if (pagesStarted) return;
  pagesStarted = true;
  for (const [name, page] of Object.entries(PAGES)) router.register(name, page);
  document.addEventListener('qtg:route', (ev) => syncDockActive(ev.detail.path));
  await router.start();
}

async function refreshAll() {
  try {
    await api.invalidateCache();
    await router.render();
    toast('已刷新', '缓存已失效并重新拉取', 'ok');
  } catch (e) {
    toast('刷新失败', e.message, 'bad');
  }
}

/* ---------------- 全局快捷键 / 错误 ---------------- */
document.addEventListener('keydown', (ev) => {
  const mod = ev.ctrlKey || ev.metaKey;
  if (mod && ev.key.toLowerCase() === 'k') {
    ev.preventDefault();
    if (pagesStarted) openPalette();
  }
});

window.addEventListener('qtg:unauthorized', () => {
  pagesStarted = false;
  showLogin('会话已过期，请重新登录');
});
window.addEventListener('unhandledrejection', (ev) => {
  if (ev.reason instanceof ApiError && ev.reason.status === 401) return;
  console.error('[qtg] unhandled', ev.reason);
});

/* ---------------- boot ---------------- */
async function boot() {
  document.getElementById('boot-tip')?.remove();
  initTheme();
  $('#loginForm')?.addEventListener('submit', doLogin);
  $('#themeBtn')?.addEventListener('click', toggleTheme);
  $('#refreshBtn')?.addEventListener('click', refreshAll);
  $('#paletteBtn')?.addEventListener('click', openPalette);
  $('#logoutBtn')?.addEventListener('click', async () => {
    const ok = await confirmDialog('退出登录', '确认退出当前会话？');
    if (ok) doLogout();
  });
  $('#adminBtn')?.addEventListener('click', () => {
    modal({
      title: '管理员',
      body: el('div', { class: 'stack' },
        el('p', { class: 'muted sm', text: '当前会话为管理员（8 小时有效）。管理令牌保存在服务器：' }),
        el('pre', { class: 'mono-sm', style: 'margin:0;padding:10px;background:var(--muted);border-radius:10px', text: '/opt/qtg/secrets/admin_token\n/opt/preview/token（兼容）' }),
        el('p', { class: 'xs muted', text: '所有写操作都会写入哈希链审计，可在「审计」页查看。' }),
      ),
      actions: [
        { label: '查看审计', onClick: () => router.navigate('audit') },
        { label: '关闭' },
      ],
    });
  });

  try {
    const session = await api.session();
    if (session?.authenticated) {
      showShell();
      await bootPages();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
}

boot();
