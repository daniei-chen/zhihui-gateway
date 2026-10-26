/* 任务：双平台调度器与 systemd 定时器。 */
import { api } from '../api.js';
import { api as _api } from '../api.js';
import { badge, card, el, renderPage, setHead, table, toast } from '../ui.js';

function schedulerCard(title, sched) {
  const tasks = sched?.tasks || [];
  return card(title, el('div', { class: 'stack' },
    el('div', { class: 'row', style: 'gap:16px' },
      el('div', {}, el('div', { class: 'xs muted', text: '状态' }), badge(sched?.enabled === false ? '暂停' : '启用', sched?.enabled === false ? 'warn' : 'ok')),
      el('div', {}, el('div', { class: 'xs muted', text: '下次' }), el('div', { class: 'sm', text: sched?.next_run || '—' })),
      el('div', {}, el('div', { class: 'xs muted', text: '上次' }), el('div', { class: 'sm', text: sched?.last_run || '—' })),
      sched?.mode ? el('div', {}, el('div', { class: 'xs muted', text: '模式' }), el('div', { class: 'sm', text: sched.mode })) : null,
    ),
    tasks.length
      ? table([
          { title: '任务', render: (t) => t.name || t.title || t.id || JSON.stringify(t).slice(0, 40) },
          { title: '计划', render: (t) => t.schedule || t.cron || t.time || '—' },
          { title: '最近', render: (t) => t.last_run || t.last || t.lastRun || '—' },
        ], tasks, { empty: '暂无任务' })
      : el('p', { class: 'muted sm', text: '该平台未上报任务列表' }),
  ));
}

async function load() {
  let data;
  try {
    data = await api.tasks();
  } catch (e) {
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  setHead('任务与调度', `${(data.systemd || []).length} 个 systemd 定时器`,
    el('button', {
      class: 'btn sm primary', type: 'button', text: '立即签到',
      onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = '执行中…';
        try { const r = await _api.checkin('all'); toast('已执行', JSON.stringify(r.data || {}).slice(0, 120), 'ok'); await load(); }
        catch (e) { toast('失败', e.message, 'bad'); }
        finally { b.disabled = false; b.textContent = '立即签到'; }
      },
    }),
  );
  renderPage(
    el('div', { class: 'grid cols-2' },
      schedulerCard('Qoder 调度器', data.qoder),
      schedulerCard('Trae 调度器', data.trae),
    ),
    card('systemd 定时器', table([
      { title: '名称', key: 'name' },
      { title: '类型', render: (r) => badge(r.platform, 'info') },
      { title: '周期', key: 'schedule' },
      { title: '状态', render: (r) => badge(r.status || 'active', 'ok') },
    ], data.systemd || [], { empty: '未发现定时器' })),
  );
}

export default async function page() {
  await load();
  return () => {};
}
