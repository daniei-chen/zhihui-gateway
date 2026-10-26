/* 请求流水：数据面每次代理调用的对账视图（谁、打哪个平台、多少 token、多久）。 */
import { api } from '../api.js';
import { badge, card, el, fmtTokens, num, renderPage, setHead, table, toast } from '../ui.js';

let platform = '';
let days = 1;
let timer = null;

function statusBadge(code) {
  const n = Number(code) || 0;
  if (!n) return badge('—', '');
  if (n < 300) return badge(String(n), 'ok');
  if (n < 500) return badge(String(n), 'warn');
  return badge(String(n), 'bad');
}

async function load(quiet = false) {
  let stats, rows;
  try {
    [stats, rows] = await Promise.all([api.proxyStats(days), api.proxyRequests(300, platform)]);
  } catch (e) {
    if (quiet) return;
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  const list = rows.requests || [];

  setHead('请求流水', `${days} 天 · 请求 ${num(stats.requests)} · Token ${fmtTokens(stats.tokens)} · 错误 ${num(stats.errors)}`,
    el('select', {
      'aria-label': '时间范围',
      onchange: (ev) => { days = Number(ev.target.value); load(); },
      html: [1, 7, 30, 90].map((d) => `<option value="${d}"${d === days ? ' selected' : ''}>近 ${d} 天</option>`).join(''),
    }),
    el('select', {
      'aria-label': '平台',
      onchange: (ev) => { platform = ev.target.value; load(); },
      html: [['', '全部平台'], ['qoder', 'Qoder'], ['trae', 'Trae']].map(([v, t]) => `<option value="${v}"${v === platform ? ' selected' : ''}>${t}</option>`).join(''),
    }),
    el('button', {
      class: 'btn sm danger', type: 'button', text: `清理 ${days} 天前`,
      onclick: async () => {
        try { const r = await api.pruneProxy(30); toast('已清理', `${r.data?.deleted ?? 0} 条`, 'ok'); await load(); }
        catch (e) { toast('清理失败', e.message, 'bad'); }
      },
    }),
  );

  renderPage(
    el('div', { class: 'grid stats' },
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '请求数' }), el('div', { class: 'stat-value', text: num(stats.requests) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: 'Token' }), el('div', { class: 'stat-value', text: fmtTokens(stats.tokens) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '错误' }), el('div', { class: `stat-value ${stats.errors ? 'bad' : ''}`.trim(), text: num(stats.errors) })),
      ...(stats.by_platform || []).map((p) => el('div', { class: 'stat' },
        el('div', { class: 'stat-label', text: p.platform === 'qoder' ? 'Qoder 请求' : 'Trae 请求' }),
        el('div', { class: 'stat-value', text: num(p.c) }),
        el('div', { class: 'stat-sub', text: `${fmtTokens(p.t)} tokens` }))),
    ),
    el('div', { class: 'grid cols-2' },
      card('按密钥', table([
        { title: '密钥', render: (r) => r.key_name || '（未命名）' },
        { title: '请求', num: true, render: (r) => num(r.c) },
        { title: 'Token', num: true, render: (r) => fmtTokens(r.t) },
      ], stats.by_key || [], { empty: '暂无数据' })),
      card('按模型', table([
        { title: '模型', render: (r) => el('span', { class: 'mono-sm', text: r.model || '—' }) },
        { title: '请求', num: true, render: (r) => num(r.c) },
        { title: 'Token', num: true, render: (r) => fmtTokens(r.t) },
        { title: '平均耗时', num: true, render: (r) => `${num(Math.round(r.d || 0))} ms` },
      ], stats.by_model || [], { empty: '暂无数据' })),
    ),
    card('明细', table([
      { title: '时间', render: (r) => el('span', { class: 'xs muted nowrap', text: r.ts }) },
      { title: '平台', render: (r) => badge(r.platform || '—', 'info') },
      { title: '模型', render: (r) => el('span', { class: 'mono-sm', text: r.model }) },
      { title: '密钥', render: (r) => el('span', { class: 'xs', text: r.key_name || '—' }) },
      { title: '状态', render: (r) => statusBadge(r.status) },
      { title: 'Prompt', num: true, render: (r) => num(r.prompt_tokens) },
      { title: 'Completion', num: true, render: (r) => num(r.completion_tokens) },
      { title: '耗时', num: true, render: (r) => (r.duration_ms ? `${num(r.duration_ms)} ms` : '—') },
      { title: '首字', num: true, render: (r) => (r.ttft_ms ? `${num(r.ttft_ms)} ms` : '—') },
      { title: '路由', render: (r) => el('span', { class: 'xs muted', text: r.route_reason || '—' }) },
    ], list, { empty: '还没有代理请求。去 Playground 跑一次试试', maxHeight: '60vh' })),
  );
}

export default async function page() {
  await load();
  timer = setInterval(() => { if (!document.hidden) load(true); }, 15000);
  return () => { if (timer) clearInterval(timer); };
}
