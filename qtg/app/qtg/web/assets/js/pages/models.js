/* 模型：双平台模型目录与能力标注。 */
import { api } from '../api.js';
import { badge, card, el, renderPage, setHead, table } from '../ui.js';

let filter = 'all';

function columns() {
  return [
    { title: '模型', render: (r) => el('div', {},
        el('div', { style: 'font-weight:600', text: r.name || r.id }),
        el('div', { class: 'xs muted mono-sm', text: r.id }),
      ) },
    { title: '平台', render: (r) => badge(r.platform === 'qoder' ? 'Qoder' : 'Trae', 'info') },
    { title: '上下文', num: true, render: (r) => (r.context_length ? r.context_length.toLocaleString() : '—') },
    { title: '最大输出', num: true, render: (r) => (r.max_tokens ? r.max_tokens.toLocaleString() : '—') },
    { title: '能力', render: (r) => el('div', { class: 'row', style: 'gap:4px' },
        r.vision ? badge('视觉', 'ok') : null,
        r.tool_calls ? badge('工具', 'info') : null,
        r.reasoning ? badge('推理', 'warn') : null,
      ) },
    { title: '状态', render: (r) => (r.enabled ? badge('可用', 'ok') : badge('停用', '')) },
  ];
}

async function load() {
  let data;
  try {
    data = await api.models();
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  const rows = [...(data.qoder || []), ...(data.trae || [])].filter((r) => filter === 'all' || r.platform === filter);
  setHead('模型目录', `Qoder ${data.qoder.length} 个 · Trae ${data.trae.length} 个`,
    el('div', { class: 'tabs' }, ...[['all', '全部'], ['qoder', 'Qoder'], ['trae', 'Trae']].map(([k, label]) =>
      el('button', {
        class: 'tab', type: 'button', 'aria-selected': filter === k ? 'true' : 'false', text: label,
        onclick: () => { filter = k; load(); },
      }))),
  );
  renderPage(card('模型列表', table(columns(), rows, { empty: '暂无模型数据', maxHeight: '70vh' })));
}

export default async function page() {
  await load();
  return () => {};
}
