/* 用量：日趋势、按模型、按密钥、请求明细。 */
import { api } from '../api.js';
import { card, el, fmtTokens, num, renderPage, setHead, table, toast } from '../ui.js';
import { lineChart, legend } from '../charts.js';

let days = 14;
let data = null;

function mergeTrend(usage) {
  const map = new Map();
  for (const p of usage.points?.qoder || []) {
    const row = map.get(p.date) || { qoder: 0, trae: 0, tokens: 0 };
    row.qoder = p.requests;
    row.tokens += Number(p.total_tokens) || 0;
    map.set(p.date, row);
  }
  for (const p of usage.points?.trae || []) {
    const row = map.get(p.date) || { qoder: 0, trae: 0, tokens: 0 };
    row.trae = p.requests;
    row.tokens += Number(p.total_tokens) || 0;
    map.set(p.date, row);
  }
  const dates = [...map.keys()].sort().slice(-days);
  return {
    labels: dates.map((d) => d.slice(5).replace('-', '/')),
    qoder: dates.map((d) => map.get(d)?.qoder || 0),
    trae: dates.map((d) => map.get(d)?.trae || 0),
    tokens: dates.map((d) => map.get(d)?.tokens || 0),
  };
}

async function loadRequests() {
  const holder = document.querySelector('#requestRows');
  if (!holder) return;
  holder.replaceChildren(el('p', { class: 'muted sm', text: '加载中…' }));
  try {
    const r = await api.requests(3);
    holder.replaceChildren(table([
      { title: '时间', key: 'ts' },
      { title: '平台', key: 'platform' },
      { title: '模型', key: 'model' },
      { title: '密钥', key: 'key_id' },
      { title: 'Prompt', key: 'prompt_tokens', num: true },
      { title: 'Completion', key: 'completion_tokens', num: true },
      { title: '积分', num: true, render: (x) => (x.credit == null ? '—' : num(x.credit, 4)) },
      { title: '耗时', num: true, render: (x) => (x.elapsed_ms == null ? '—' : `${num(x.elapsed_ms)} ms`) },
    ], r.rows || [], { empty: '暂无请求明细' }));
  } catch (e) {
    holder.replaceChildren(el('p', { class: 'err', text: e.message }));
  }
}

async function load(force = false) {
  try {
    data = await api.usage(days);
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  const trend = mergeTrend(data);
  const chart = lineChart({
    labels: trend.labels, height: 220,
    series: [
      { name: 'Qoder 请求', values: trend.qoder, cls: 'series-1', area: true },
      { name: 'Trae 请求', values: trend.trae, cls: 'series-2' },
    ],
  });

  const byModel = (data.by_model || []).slice(0, 20);
  const byKeyMap = new Map();
  for (const m of byModel) {
    // 按模型聚合（跨平台同名合并）
    const row = byKeyMap.get(m.model) || { model: m.model, requests: 0, tokens: 0, credit: 0, platforms: new Set() };
    row.requests += m.requests || 0;
    row.tokens += m.tokens || 0;
    row.credit += Number(m.credit) || 0;
    row.platforms.add(m.platform);
    byKeyMap.set(m.model, row);
  }
  const modelRows = [...byKeyMap.values()].sort((a, b) => b.requests - a.requests);

  const totalTokens = trend.tokens.reduce((a, b) => a + b, 0);
  const totalReq = trend.qoder.reduce((a, b) => a + b, 0) + trend.trae.reduce((a, b) => a + b, 0);

  setHead('用量', `${days} 天 · 请求 ${num(totalReq)} · Token ${fmtTokens(totalTokens)}`,
    el('select', {
      'aria-label': '时间范围',
      onchange: (ev) => { days = Number(ev.target.value); load(true); },
      html: [7, 14, 30, 60, 90].map((d) => `<option value="${d}"${d === days ? ' selected' : ''}>近 ${d} 天</option>`).join(''),
    }),
  );

  renderPage(
    el('div', { class: 'grid stats' },
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '总请求' }), el('div', { class: 'stat-value', text: num(totalReq) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '总 Token' }), el('div', { class: 'stat-value', text: fmtTokens(totalTokens) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '今日请求' }), el('div', { class: 'stat-value', text: num(data.totals?.today_requests || 0) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '今日 Token' }), el('div', { class: 'stat-value', text: fmtTokens((data.totals?.today_prompt_tokens || 0) + (data.totals?.today_completion_tokens || 0)) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '今日错误' }), el('div', { class: `stat-value ${(data.totals?.today_errors || 0) > 0 ? 'bad' : ''}`.trim(), text: num(data.totals?.today_errors || 0) })),
    ),
    card('请求趋势', el('div', {}, chart, legend([
      { name: 'Qoder', color: 'var(--chart-1)' },
      { name: 'Trae', color: 'var(--chart-2)' },
    ]))),
    el('div', { class: 'grid cols-2' },
      card('按模型', table([
        { title: '模型', key: 'model' },
        { title: '平台', key: 'platforms', render: (r) => [...r.platforms].map((p) => (p === 'qoder' ? 'Qoder' : 'Trae')).join(' / ') },
        { title: '请求', key: 'requests', num: true, render: (r) => num(r.requests) },
        { title: 'Token', num: true, render: (r) => fmtTokens(r.tokens) },
        { title: '积分', num: true, render: (r) => num(r.credit, 4) },
      ], modelRows, { empty: '暂无模型数据', maxHeight: '360px' })),
      card('请求明细（近 3 天）', el('div', { id: 'requestRows' })),
    ),
  );
  loadRequests();
}

export default async function page() {
  await load(false);
  return () => { /* 无清理 */ };
}
