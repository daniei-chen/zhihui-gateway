/* 告警：上游告警列表 + 一键标记已读。 */
import { api } from '../api.js';
import { badge, card, confirmDialog, el, renderPage, setHead, table, toast } from '../ui.js';

function severityKind(sev) {
  const s = String(sev || '').toLowerCase();
  if (s === 'error' || s === 'critical' || s === 'danger') return 'bad';
  if (s === 'warn' || s === 'warning') return 'warn';
  return 'info';
}

async function load() {
  let data;
  try {
    data = await api.alerts();
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  const rows = data.alerts || [];
  const unread = rows.filter((a) => !a.acked).length;
  setHead('告警', `${rows.length} 条 · 未读 ${unread}`,
    el('button', {
      class: 'btn sm', type: 'button', text: '全部标记已读',
      onclick: async () => {
        const ok = await confirmDialog('标记已读', '确认把上游告警全部标记为已读？');
        if (!ok) return;
        try { await api.ackAlerts(); toast('已标记', '', 'ok'); await load(); }
        catch (e) { toast('操作失败', e.message, 'bad'); }
      },
    }),
  );
  renderPage(card('告警列表', table([
    { title: '时间', key: 'ts' },
    { title: '平台', render: (r) => badge(r.platform === 'qoder' ? 'Qoder' : 'Trae', 'info') },
    { title: '级别', render: (r) => badge(r.severity, severityKind(r.severity)) },
    { title: '标题', key: 'title' },
    { title: '详情', render: (r) => el('span', { class: 'xs muted', text: r.detail || '' }) },
    { title: '状态', render: (r) => (r.acked ? badge('已读', '') : badge('未读', 'warn')) },
  ], rows, { empty: '暂无告警', maxHeight: '70vh' })));
}

export default async function page() {
  await load();
  return () => {};
}
