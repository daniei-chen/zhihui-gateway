/* 审计：哈希链完整性 + 每条写操作留痕。 */
import { api } from '../api.js';
import { badge, card, el, renderPage, setHead, table } from '../ui.js';

function resultKind(r) {
  return r === 'ok' ? 'ok' : r === 'denied' ? 'warn' : 'bad';
}

async function load() {
  let data;
  try {
    data = await api.audit(200);
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  const integ = data.integrity || {};
  setHead('审计日志', `最近 ${data.records.length} 条 · 链完整性 ${integ.ok ? '正常' : '已损坏'}`,
    el('button', { class: 'btn sm', type: 'button', text: '重新校验', onclick: () => load() }),
  );
  renderPage(
    el('div', { class: 'grid stats' },
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '审计记录' }), el('div', { class: 'stat-value', text: String(integ.records ?? data.records.length) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '链完整性' }), el('div', { class: `stat-value ${integ.ok ? 'ok' : 'bad'}`.trim(), text: integ.ok ? '正常' : '损坏' }), el('div', { class: 'stat-sub', text: integ.broken_at ? `断点 #${integ.broken_at}` : '无断点' })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '写操作' }), el('div', { class: 'stat-value', text: String(data.records.filter((r) => r.action !== 'login' && r.action !== 'logout').length) })),
      el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: '被拒' }), el('div', { class: 'stat-value', text: String(data.records.filter((r) => r.result !== 'ok').length) })),
    ),
    card('审计链', table([
      { title: '时间', render: (r) => el('span', { class: 'xs muted nowrap', text: r.ts }) },
      { title: '动作', render: (r) => el('span', { class: 'mono-sm', text: r.action }) },
      { title: '目标', render: (r) => el('span', { class: 'xs', text: r.target || '—' }) },
      { title: '结果', render: (r) => badge(r.result, resultKind(r.result)) },
      { title: '来源', render: (r) => el('span', { class: 'xs muted', text: r.ip || r.actor || '—' }) },
      { title: '摘要', render: (r) => el('span', { class: 'xs muted', text: r.hash ? r.hash.slice(0, 12) + '…' : '—' }) },
    ], [...data.records].reverse(), { empty: '暂无审计记录', maxHeight: '66vh' }), { sub: '每条记录的 hash = sha256(去 hash 后的内容)，prev 指向上一条' }),
  );
}

export default async function page() {
  await load();
  return () => {};
}
