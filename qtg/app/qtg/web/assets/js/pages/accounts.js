/* 账号：双平台列表、状态筛选、启停/刷新/新增（OAuth 与凭据导入）。 */
import { api } from '../api.js';
import {
  $, badge, card, confirmDialog, el, heartbeat, modal, num, renderPage,
  setHead, stateBadge, table, toast,
} from '../ui.js';

let tab = 'all';
let keyword = '';
let cache = null;
let pollTimer = null;

function filtered(rows) {
  const kw = keyword.trim().toLowerCase();
  return rows.filter((a) => {
    if (tab !== 'all' && a.platform !== tab) return false;
    if (!kw) return true;
    return `${a.name} ${a.uid} ${a.realm} ${a.state}`.toLowerCase().includes(kw);
  });
}

function accountRow(a) {
  const canToggle = true;
  return el('tr', {},
    el('td', {},
      el('div', { class: 'row', style: 'gap:8px' },
        el('div', { class: 'account-chip', style: 'width:26px;height:26px;font-size:11px', text: (a.name || '?').slice(0, 1) }),
        el('div', {},
          el('div', { style: 'font-weight:600', text: a.name || a.uid.slice(0, 8) }),
          el('div', { class: 'xs muted mono-sm', text: a.uid }),
        ),
      ),
    ),
    el('td', {}, badge(a.platform === 'qoder' ? 'Qoder' : 'Trae', 'info')),
    el('td', { text: a.realm || '—' }),
    el('td', {}, stateBadge(a.state)),
    el('td', { class: 'num' },
      el('div', { text: `${num(a.credits_remain, 2)} / ${num(a.credits_total, 2)}` }),
      el('div', { class: 'xs muted', text: a.plan || '' }),
    ),
    el('td', { class: 'num', text: a.days_left == null ? '长期' : a.days_left <= 0 ? '已过期' : `${a.days_left} 天` }),
    el('td', {},
      el('div', { class: 'row', style: 'gap:4px' },
        el('button', {
          class: 'btn sm', type: 'button', text: '刷新',
          onclick: async (ev) => {
            const b = ev.currentTarget; b.disabled = true;
            try { await api.accountAction(a.platform, 'refresh', a.uid); toast('已刷新', `${a.name}`, 'ok'); await load(true); }
            catch (e) { toast('刷新失败', e.message, 'bad'); b.disabled = false; }
          },
        }),
        canToggle ? el('button', {
          class: `btn sm ${a.enabled ? 'danger' : ''}`.trim(), type: 'button',
          text: a.enabled ? '停用' : '启用',
          onclick: async (ev) => {
            const ok = await confirmDialog(
              a.enabled ? '停用账号' : '启用账号',
              `确认${a.enabled ? '停用' : '启用'}「${a.name || a.uid}」？`,
              { danger: a.enabled, confirmText: a.enabled ? '停用' : '启用' },
            );
            if (!ok) return;
            const b = ev.currentTarget; b.disabled = true;
            try { await api.accountAction(a.platform, a.enabled ? 'disable' : 'enable', a.uid); toast('操作完成', '', 'ok'); await load(true); }
            catch (e) { toast('操作失败', e.message, 'bad'); b.disabled = false; }
          },
        }) : null,
      ),
    ),
  );
}

const COLUMNS = [
  { title: '账号', key: 'name' },
  { title: '平台', key: 'platform' },
  { title: '区域', key: 'realm' },
  { title: '状态', key: 'state' },
  { title: '积分', num: true },
  { title: '到期', num: true },
  { title: '操作' },
];

function openAddDialog() {
  const body = el('div', { class: 'stack' },
    el('div', { class: 'filters' },
      el('label', { class: 'check' },
        el('input', { type: 'radio', name: 'addPlatform', value: 'qoder', checked: true }),
        'Qoder（PAT）',
      ),
      el('label', { class: 'check' },
        el('input', { type: 'radio', name: 'addPlatform', value: 'trae' }),
        'Trae（JWT）',
      ),
    ),
    el('label', { class: 'field' }, el('span', { text: '备注名（可选）' }), el('input', { id: 'addName', type: 'text', placeholder: '例如：主账号' })),
    el('label', { class: 'field' }, el('span', { text: '凭据（PAT / JWT）' }), el('textarea', { id: 'addCred', placeholder: '粘贴凭据' })),
    el('p', { class: 'xs muted', text: '凭据只会提交给本机上游 BFF，不写入本控制面的审计日志（已脱敏）。' }),
  );
  modal({
    title: '新增账号',
    body,
    actions: [
      { label: '取消' },
      {
        label: '提交', kind: 'primary',
        onClick: async () => {
          const platform = document.querySelector('input[name=addPlatform]:checked')?.value || 'qoder';
          const cred = $('#addCred')?.value?.trim();
          const name = $('#addName')?.value?.trim() || '';
          if (!cred) { toast('请粘贴凭据', '', 'warn'); return true; }
          try {
            await api.addAccount(platform, cred, name, 'cn');
            toast('已提交', `${platform} 账号`, 'ok');
            await load(true);
          } catch (e) {
            toast('提交失败', e.message, 'bad');
            return true;
          }
          return false;
        },
      },
    ],
  });
}

function openOAuthDialog() {
  const body = el('div', { class: 'stack' },
    el('p', { class: 'muted sm', text: 'Qoder 支持设备授权轮询自动入池；Trae 需手动粘贴回调链接。' }),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', type: 'button', text: 'Qoder 设备授权',
        onclick: async (ev) => { ev.currentTarget.disabled = true; await startQoderOAuth(); },
      }),
      el('button', {
        class: 'btn', type: 'button', text: 'Trae 授权链接',
        onclick: async (ev) => { ev.currentTarget.disabled = true; await startTraeOAuth(); },
      }),
    ),
    el('div', { id: 'oauthPane', class: 'stack' }),
  );
  modal({ title: '授权添加账号', body, actions: [{ label: '关闭' }], onClose: () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } } });
}

async function startQoderOAuth() {
  const pane = $('#oauthPane');
  pane.replaceChildren(el('p', { class: 'muted sm', text: '正在生成授权链接…' }));
  try {
    const r = await api.oauthStart('qoder', 'cn');
    const url = r.data?.authUrl || r.data?.url || '';
    const state = r.data?.state || '';
    pane.replaceChildren(
      el('p', { class: 'sm', text: '在浏览器打开下面的链接完成设备授权：' }),
      el('div', { class: 'row' },
        el('button', { class: 'btn sm primary', type: 'button', text: '打开链接', onclick: () => window.open(url, '_blank', 'noopener') }),
        el('button', {
          class: 'btn sm', type: 'button', text: '复制链接',
          onclick: async () => { try { await navigator.clipboard.writeText(url); toast('已复制', '', 'ok'); } catch { toast('复制失败', '请手动选择', 'warn'); } },
        }),
      ),
      el('p', { class: 'xs muted mono-sm', text: url }),
      el('p', { class: 'sm', id: 'oauthStatus', text: '等待授权…' }),
    );
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const p = await api.oauthPoll('qoder', state);
        const st = $('#oauthStatus');
        if (st) st.textContent = p.data?.message || p.data?.status || '等待授权…';
        const msg = JSON.stringify(p.data || {});
        if (/success|done|ok|complete|成功|已入池/i.test(msg)) {
          clearInterval(pollTimer); pollTimer = null;
          toast('账号已入池', 'Qoder', 'ok');
          await load(true);
        }
      } catch (e) {
        const st = $('#oauthStatus');
        if (st) st.textContent = '轮询失败：' + e.message;
      }
    }, 2500);
  } catch (e) {
    pane.replaceChildren(el('p', { class: 'err', text: e.message }));
  }
}

async function startTraeOAuth() {
  const pane = $('#oauthPane');
  pane.replaceChildren(el('p', { class: 'muted sm', text: '正在生成授权链接…' }));
  try {
    const r = await api.oauthStart('trae', 'cn');
    const url = r.data?.url || r.data?.data?.url || '';
    pane.replaceChildren(
      el('p', { class: 'sm', text: '打开链接授权后，把浏览器地址栏整条回调链接粘贴到下面：' }),
      el('div', { class: 'row' },
        el('button', { class: 'btn sm primary', type: 'button', text: '打开链接', onclick: () => window.open(url, '_blank', 'noopener') }),
      ),
      el('label', { class: 'field' }, el('span', { text: '回调链接' }), el('textarea', { id: 'traeCallback', placeholder: 'http://127.0.0.1:xxxx/authorize?code=...' })),
      el('button', {
        class: 'btn primary', type: 'button', text: '完成登录',
        onclick: async () => {
          const cb = $('#traeCallback')?.value?.trim();
          if (!cb) { toast('请粘贴回调链接', '', 'warn'); return; }
          try { await api.oauthFinish(cb); toast('账号已添加', 'Trae', 'ok'); await load(true); }
          catch (e) { toast('登录失败', e.message, 'bad'); }
        },
      }),
    );
  } catch (e) {
    pane.replaceChildren(el('p', { class: 'err', text: e.message }));
  }
}

let rendered = false;

async function load(force = false, quiet = false) {
  let data;
  try {
    data = await api.accounts(force);
  } catch (e) {
    if (quiet && rendered) { toast('刷新失败', e.message, 'bad'); return; }
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  rendered = true;
  cache = data;
  const rows = filtered([...(data.qoder || []), ...(data.trae || [])]);
  const s = data.summary?.all || {};

  const tabs = el('div', { class: 'tabs', role: 'tablist' },
    ...[['all', `全部 ${s.total || 0}`], ['qoder', `Qoder ${data.summary?.qoder?.total || 0}`], ['trae', `Trae ${data.summary?.trae?.total || 0}`]].map(([k, label]) =>
      el('button', {
        class: 'tab', type: 'button', role: 'tab', 'aria-selected': tab === k ? 'true' : 'false', text: label,
        onclick: () => { tab = k; load(false); },
      })),
  );

  const search = el('input', {
    type: 'search', placeholder: '搜索账号 / UID / 区域', value: keyword,
    oninput: (ev) => {
      keyword = ev.target.value;
      const list = filtered([...(cache.qoder || []), ...(cache.trae || [])]);
      const holder = $('#accountTable');
      if (holder) holder.replaceChildren(table(COLUMNS, list, { empty: '没有匹配的账号' }));
    },
  });

  setHead('账号', `共 ${s.total || 0} 个 · 正常 ${s.ready || 0} · 冷却 ${s.cooling || 0} · 停用 ${s.disabled || 0}`,
    el('button', { class: 'btn sm', type: 'button', text: '授权添加', onclick: openOAuthDialog }),
    el('button', { class: 'btn sm primary', type: 'button', text: '新增账号', onclick: openAddDialog }),
  );

  const tableHolder = el('div', { id: 'accountTable' }, table(COLUMNS, rows, { empty: '没有匹配的账号' }));

  renderPage(
    el('div', { class: 'card' },
      el('div', { class: 'filters', style: 'justify-content:space-between' }, tabs, search),
      tableHolder,
    ),
    el('div', { class: 'grid cols-3' },
      el('div', { class: 'card' }, el('div', { class: 'stat-label', text: 'Qoder 账号' }), el('div', { class: 'stat-value', text: String(data.summary?.qoder?.total || 0) }), el('div', { class: 'stat-sub', text: `积分 ${num(data.summary?.qoder?.credits || 0, 2)}` })),
      el('div', { class: 'card' }, el('div', { class: 'stat-label', text: 'Trae 账号' }), el('div', { class: 'stat-value', text: String(data.summary?.trae?.total || 0) }), el('div', { class: 'stat-sub', text: `积分 ${num(data.summary?.trae?.credits || 0, 2)}` })),
      el('div', { class: 'card' }, el('div', { class: 'stat-label', text: '即将过期' }), el('div', { class: 'stat-value', text: String(s.expiring_soon || 0) }), el('div', { class: 'stat-sub', text: '3 天内到期' })),
    ),
  );
}

export default async function page() {
  rendered = false;
  await load(false);
  const onQuickAdd = () => openAddDialog();
  window.addEventListener('qtg:quick-add', onQuickAdd);
  const stopHeartbeat = heartbeat(() => load(true, true), 45000);
  return () => {
    stopHeartbeat();
    window.removeEventListener('qtg:quick-add', onQuickAdd);
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };
}
