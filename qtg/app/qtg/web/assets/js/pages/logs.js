/* 日志：双平台运行日志，支持关键字过滤与级别筛选。 */
import { api } from '../api.js';
import { badge, card, el, renderPage, setHead } from '../ui.js';

let data = null;
let keyword = '';
let level = '';

function levelKind(lv) {
  const s = String(lv || '').toLowerCase();
  if (s.startsWith('err') || s === 'error') return 'bad';
  if (s.startsWith('warn')) return 'warn';
  if (s.startsWith('info')) return 'info';
  return '';
}

function row(l) {
  return el('div', { class: 'list-item' },
    el('span', { class: 'dot', style: `background:${levelKind(l.level) === 'bad' ? 'var(--destructive)' : levelKind(l.level) === 'warn' ? 'var(--warn)' : 'var(--muted-foreground)'}` }),
    el('span', { class: 'xs muted nowrap', text: l.msg_time || l.ts || '' }),
    badge(l.platform === 'qoder' ? 'Q' : 'T', 'info'),
    el('span', { class: 'grow ellipsis mono-sm', text: l.message || '' }),
    l.level ? badge(l.level, levelKind(l.level)) : null,
  );
}

function filtered(rows) {
  const kw = keyword.trim().toLowerCase();
  return rows.filter((l) => {
    if (level && String(l.level || '').toLowerCase() !== level) return false;
    if (!kw) return true;
    return String(l.message || '').toLowerCase().includes(kw);
  });
}

function paint() {
  const holder = document.querySelector('#logRows');
  if (!holder || !data) return;
  const rows = filtered([...(data.qoder || []), ...(data.trae || [])]);
  holder.replaceChildren(...(rows.length
    ? rows.slice(0, 400).map(row)
    : [el('div', { class: 'empty', text: '没有匹配的日志' })]));
  const counter = document.querySelector('#logCount');
  if (counter) counter.textContent = `${rows.length} 条`;
}

async function load() {
  try {
    data = await api.logs();
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  setHead('运行日志', `Qoder ${data.qoder.length} 条 · Trae ${data.trae.length} 条`);
  renderPage(
    card('全部日志', el('div', {},
      el('div', { class: 'filters', style: 'margin-bottom:10px' },
        el('input', {
          type: 'search', placeholder: '过滤关键字', value: keyword,
          oninput: (ev) => { keyword = ev.target.value; paint(); },
        }),
        el('select', {
          'aria-label': '级别',
          onchange: (ev) => { level = ev.target.value; paint(); },
          html: [['', '全部级别'], ['info', 'info'], ['warn', 'warn'], ['error', 'error']]
            .map(([v, t]) => `<option value="${v}"${v === level ? ' selected' : ''}>${t}</option>`).join(''),
        }),
        el('span', { class: 'xs muted', id: 'logCount', text: '' }),
      ),
      el('div', { class: 'list scroll-y', id: 'logRows' }),
    ), { sub: '最多展示 400 条' }),
  );
  paint();
}

export default async function page() {
  await load();
  return () => {};
}
