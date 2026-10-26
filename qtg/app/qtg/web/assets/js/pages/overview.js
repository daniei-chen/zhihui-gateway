/* 概览：统计卡 + 请求/失败趋势 + 上游 8 项明细 + 积分收支 + 账号健康 + 签到明细。
   对齐旧面板的信息密度，并补齐旧面板没有的失败趋势与账号状态徽标。 */
import { api } from '../api.js';
import {
  $, badge, card, countUp, el, fmtTokens, heartbeat, ICONS, loadingRows, num,
  renderPage, setHead, stateBadge, toast,
} from '../ui.js';
import { dualLine, legend, lineChart } from '../charts.js';

let realm = 'all';
let data = null;
let rendered = false;

function accountsByRealm() {
  if (!data) return { qoder: [], trae: [] };
  const q = data.accounts.qoder || [];
  const t = data.accounts.trae || [];
  return {
    qoder: realm === 'all' ? q : q.filter((a) => a.realm === realm),
    trae: realm === 'all' ? t : [],
  };
}

function buildTrend() {
  const q = data.usage.points?.qoder || [];
  const t = data.usage.points?.trae || [];
  const map = new Map();
  for (const p of q) map.set(p.date, { qoder: p.requests, trae: 0 });
  for (const p of t) {
    const row = map.get(p.date) || { qoder: 0, trae: 0 };
    row.trae = p.requests;
    map.set(p.date, row);
  }
  const dates = [...map.keys()].sort().slice(-14);
  return {
    dates,
    labels: dates.map((d) => d.slice(5).replace('-', '/')),
    qoder: dates.map((d) => map.get(d)?.qoder || 0),
    trae: dates.map((d) => map.get(d)?.trae || 0),
  };
}

function buildCredits() {
  const map = new Map();
  for (const c of data.usage.credits || []) {
    const row = map.get(c.date) || { income: 0, expense: 0 };
    row.income += Number(c.income) || 0;
    row.expense += Number(c.expense) || 0;
    map.set(c.date, row);
  }
  const dates = [...map.keys()].sort().slice(-7);
  return {
    labels: dates.map((d) => d.slice(5).replace('-', '/')),
    income: dates.map((d) => map.get(d)?.income || 0),
    expense: dates.map((d) => map.get(d)?.expense || 0),
    totalIn: dates.reduce((s, d) => s + (map.get(d)?.income || 0), 0),
    totalOut: dates.reduce((s, d) => s + (map.get(d)?.expense || 0), 0),
  };
}

function regionSwitch() {
  const seg = el('div', { class: 'tabs', role: 'tablist', 'aria-label': '区域筛选' });
  for (const [key, label] of [['all', '全部'], ['cn', '国内版'], ['intl', '国际版']]) {
    seg.append(el('button', {
      class: 'tab', type: 'button', role: 'tab',
      'aria-selected': realm === key ? 'true' : 'false', text: label,
      onclick: () => { realm = key; load(false); },
    }));
  }
  return seg;
}

function upstreamCard() {
  const { qoder, trae } = accountsByRealm();
  const sum = (rows) => ({
    ready: rows.filter((a) => a.state === 'ready').length,
    cooling: rows.filter((a) => a.state === 'cooling').length,
    disabled: rows.filter((a) => a.state === 'disabled').length,
  });
  const qs = sum(qoder);
  const ts = sum(trae);
  const sched = data.tasks?.qoder || {};
  const traeUp = (data.health.upstreams || []).find((u) => u.name === 'trae-bff');

  const rows = [
    ['Qoder 调度', sched.enabled === false ? '暂停' : (sched.mode || '正常'), sched.enabled !== false],
    ['Qoder 健康账号', qs.ready, qs.ready > 0],
    ['Qoder 冷却 / 禁用', `${qs.cooling} / ${qs.disabled}`, qs.cooling + qs.disabled === 0],
    ['Trae 网关', traeUp ? (traeUp.up ? 'ok' : 'down') : '—', !!traeUp?.up],
    ['Trae 可用 / 总数', `${ts.ready} / ${trae.length}`, ts.ready > 0],
    ['Trae 冷却 / 禁用', `${ts.cooling} / ${ts.disabled}`, ts.cooling + ts.disabled === 0],
    ['粘性会话', String(data.sticky_sessions ?? 0), true],
    ['存储模式', data.storage_mode || 'local-file', true],
  ];

  return card('反代上游', el('div', {},
    ...rows.map(([k, v, ok]) => el('div', { class: 'kv' },
      el('span', { class: 'k' },
        el('span', { class: 'dot', style: `background:${ok ? 'var(--ok)' : 'var(--destructive)'}` }),
        k,
      ),
      el('span', { class: 'v', text: String(v) }),
    )),
    el('div', { class: 'kv' },
      el('span', { class: 'k' }, '整体状态'),
      el('span', { class: 'v' }, data.health.overall ? badge('正常', 'ok') : badge('降级', 'warn')),
    ),
  ), { actions: data.degraded ? badge('部分数据降级', 'warn') : null });
}

function accountHealthCard() {
  const { qoder, trae } = accountsByRealm();
  const all = [...qoder, ...trae];
  if (!all.length) return card('账号健康快照', el('div', { class: 'empty', text: '当前筛选下无账号' }));
  return card('账号健康快照', el('div', {},
    el('div', { class: 'grid auto' }, ...all.map((a) => {
      const total = Number(a.credits_total) || Number(a.credits_remain) || 1;
      const ratio = Math.max(3, Math.min(100, Math.round(((Number(a.credits_remain) || 0) / total) * 100)));
      return el('div', { class: 'list-item', style: 'flex-direction:column;align-items:stretch;gap:8px' },
        el('div', { class: 'row between' },
          el('div', { class: 'row', style: 'gap:8px;min-width:0' },
            el('div', { class: 'account-chip', text: (a.name || '?').slice(0, 1) }),
            el('div', { style: 'min-width:0' },
              el('div', { class: 'ellipsis', style: 'font-weight:600', text: a.name || a.uid.slice(0, 8) }),
              el('div', { class: 'xs muted', text: `${a.platform === 'qoder' ? 'Qoder' : 'Trae'} · ${a.realm === 'intl' ? '国际版' : a.realm === 'cn' ? '国内版' : a.realm || 'Trae'} · ${String(a.uid).slice(0, 8)}…` }),
            ),
          ),
          stateBadge(a.state),
        ),
        el('div', { class: 'progress' }, el('i', { style: `width:${ratio}%` })),
        el('div', { class: 'row between xs muted' },
          el('span', { text: `${num(a.credits_remain, 2)} / ${num(total, 2)}` }),
          el('span', { text: a.days_left == null ? '长期' : a.days_left <= 0 ? '已过期' : `${a.days_left} 天到期` }),
        ),
      );
    })),
    el('div', { class: 'row end', style: 'margin-top:8px' },
      el('a', { href: '#/accounts', class: 'xs muted', text: '查看全部账号 →' }),
    ),
  ), { sub: `在线 ${all.length}` });
}

function checkinCard() {
  const sched = data.tasks?.qoder || {};
  const log = data.checkin_log || [];
  const rnd = data.random_checkin || {};
  const btn = el('button', {
    class: 'btn sm primary', type: 'button', text: '一键签到',
    onclick: async (ev) => {
      const b = ev.currentTarget;
      b.disabled = true; b.textContent = '签到中…';
      try {
        const r = await api.checkin('all');
        const q = r.data?.qoder?.results || [];
        const ok = q.filter((x) => x.ok).length;
        toast('签到完成', `Qoder ${ok}/${q.length} 成功 · Trae 已执行`, 'ok');
        await load(true, true);
      } catch (e) {
        toast('签到失败', e.message, 'bad');
      } finally {
        b.disabled = false; b.textContent = '一键签到';
      }
    },
  });

  return card('签到与调度', el('div', { class: 'stack' },
    el('div', { class: 'grid cols-2' },
      ...(log.length ? log.slice(0, 4).map((r) => el('div', { class: 'list-item' },
        badge(r.date, 'info'),
        el('span', { class: 'grow sm', text: `成功 ${r.ok} · 已签 ${r.already} · 失败 ${r.failed}` }),
        r.failed ? badge('有失败', 'bad') : badge('正常', 'ok'),
      )) : [el('div', { class: 'empty', text: '暂无签到记录' })]),
    ),
    el('div', { class: 'kv' }, el('span', { class: 'k' }, 'Qoder 下次'), el('span', { class: 'v', text: sched.next_run || '—' })),
    el('div', { class: 'kv' }, el('span', { class: 'k' }, 'Qoder 上次'), el('span', { class: 'v', text: sched.last_run || '—' })),
    el('div', { class: 'kv' }, el('span', { class: 'k' }, '今日随机'), el('span', { class: 'v', text: `Trae ${rnd.targets?.trae || '—'} · Qoder ${rnd.targets?.qoder || '—'}` })),
  ), { actions: [btn] });
}

async function load(force = false, quiet = false) {
  if (!quiet) renderPage(loadingRows(4));
  try {
    data = await api.overview(force);
  } catch (e) {
    if (quiet && rendered) { toast('刷新失败', e.message, 'bad'); return; }
    renderPage(el('div', { class: 'card' }, el('h3', { text: '加载失败' }), el('p', { class: 'muted sm', text: e.message })));
    return;
  }
  rendered = true;

  const { qoder, trae } = accountsByRealm();
  const all = [...qoder, ...trae];
  const sum = {
    total: all.length,
    ready: all.filter((a) => a.state === 'ready').length,
    cooling: all.filter((a) => a.state === 'cooling').length,
    credits: all.reduce((s, a) => s + (Number(a.credits_remain) || 0), 0),
    expiring: all.filter((a) => a.days_left != null && a.days_left >= 0 && a.days_left <= 3).length,
  };
  const trend = buildTrend();
  const credits = buildCredits();
  const totalTokens = (data.usage.points?.qoder || []).reduce((s, p) => s + (p.total_tokens || 0), 0)
    + (data.usage.points?.trae || []).reduce((s, p) => s + (p.total_tokens || 0), 0);
  const qCredits = qoder.reduce((s, a) => s + (a.credits_remain || 0), 0);
  const tCredits = trae.reduce((s, a) => s + (a.credits_remain || 0), 0);

  // 语义色调 + 图标：对齐 WorkBuddy StatCard 的 TONE_VALUE / TONE_ICON 思路
  const stats = [
    { label: '账号总数', raw: sum.total, fmt: (v) => String(Math.round(v)), icon: 'users', tone: '' },
    { label: '有效期内', raw: sum.ready, fmt: (v) => String(Math.round(v)), icon: 'check', tone: sum.ready === sum.total ? '' : 'warn',
      sub: sum.ready === sum.total ? '全部正常' : `冷却 ${sum.cooling}` },
    { label: '即将过期', raw: sum.expiring, fmt: (v) => String(Math.round(v)), icon: 'warn', tone: sum.expiring ? 'bad' : '',
      sub: sum.expiring ? '3 天内到期积分' : '暂无风险' },
    { label: '积分余额', raw: sum.credits, fmt: (v) => num(v, 0), icon: 'key', tone: '',
      sub: `Qoder ${num(qCredits, 0)} · Trae ${num(tCredits, 0)}` },
    { label: '14 天 Token', raw: totalTokens, fmt: (v) => fmtTokens(v), icon: 'chart', tone: '',
      sub: `今日请求 ${data.usage.totals?.today_requests || 0}` },
  ];
  stats[0].sub = `Qoder ${qoder.length} · Trae ${trae.length}`;

  const trendChart = lineChart({
    labels: trend.labels, height: 220,
    series: [
      { name: 'Qoder', values: trend.qoder, cls: 'series-1', area: true },
      { name: 'Trae', values: trend.trae, cls: 'series-2' },
      { name: '失败', values: (data.failures && data.failures.length) ? data.failures : trend.labels.map(() => 0), cls: 'series-bad' },
    ],
  });

  const creditChart = dualLine({ labels: credits.labels, a: credits.income, b: credits.expense, height: 200 });

  setHead('概览', `账号池健康度、反代网关与今日用量 · 更新于 ${data.generated_at}`,
    regionSwitch(),
    el('button', {
      class: 'btn sm', type: 'button', text: '刷新',
      onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true; b.textContent = '刷新中…';
        try { await api.invalidateCache(); await load(true); toast('已刷新', '', 'ok', 1200); }
        catch (e) { toast('刷新失败', e.message, 'bad'); }
        finally { b.disabled = false; b.textContent = '刷新'; }
      },
    }),
  );

  renderPage(
    data.degraded ? el('div', { class: 'card plain' },
      el('div', { class: 'row between' },
        el('div', {}, el('h3', { text: '部分数据降级' }), el('p', { class: 'muted sm', text: (data.errors || []).slice(0, 2).join('；') })),
        badge('degraded', 'warn'),
      )) : null,
    el('div', { class: 'grid stats' }, ...stats.map((st) =>
      el('div', { class: 'stat' },
        el('div', { class: 'stat-top' },
          el('div', { class: 'stat-label', text: st.label }),
          el('div', { class: 'stat-icon', 'data-tone': st.tone || 'info', html: ICONS[st.icon] || ICONS.dot }),
        ),
        el('div', {},
          el('div', { class: `stat-value ${st.tone === 'bad' ? 'bad' : ''}`.trim(), 'data-raw': String(st.raw || 0) }),
          st.sub ? el('div', { class: 'stat-sub', text: st.sub }) : null,
        ),
      ))),
    el('div', { class: 'grid cols-2' },
      card('近 14 天调用趋势', el('div', {},
        el('div', { class: 'row between xs muted', style: 'margin-bottom:6px' },
          el('span', { text: `近 7 天 · ${data.failure_total ?? 0} 次失败` }),
          el('a', { href: '#/usage', class: 'muted', text: '详细用量 →' }),
        ),
        trendChart,
        legend([
          { name: 'Qoder', color: 'var(--chart-1)' },
          { name: 'Trae', color: 'var(--chart-2)' },
          { name: '失败', color: 'var(--destructive)' },
        ]),
      )),
      upstreamCard(),
    ),
    card('积分收支', el('div', { class: 'stack' },
      el('div', { class: 'grid cols-3' },
        el('div', { class: 'list-item', style: 'flex-direction:column;align-items:flex-start' },
          el('div', { class: 'xs muted', text: '收入' }),
          el('div', { style: 'font-size:18px;font-weight:600;color:var(--chart-2)', text: '+' + num(credits.totalIn, 0) }),
        ),
        el('div', { class: 'list-item', style: 'flex-direction:column;align-items:flex-start' },
          el('div', { class: 'xs muted', text: '支出' }),
          el('div', { style: 'font-size:18px;font-weight:600;color:var(--destructive)', text: '-' + num(credits.totalOut, 2) }),
        ),
        el('div', { class: 'list-item', style: 'flex-direction:column;align-items:flex-start' },
          el('div', { class: 'xs muted', text: '净增' }),
          el('div', { style: 'font-size:18px;font-weight:600', text: (credits.totalIn - credits.totalOut >= 0 ? '+' : '') + num(credits.totalIn - credits.totalOut, 2) }),
        ),
      ),
      creditChart,
      legend([{ name: '收入', color: 'var(--chart-2)' }, { name: '支出', color: 'var(--destructive)' }]),
    ), { sub: '近 7 天' }),
    accountHealthCard(),
    checkinCard(),
  );
  // 数字滚动：motion 的 inView 触发（对齐 counting-number 的 useInView）
  document.querySelectorAll('#page .stat-value').forEach((node, i) => {
    const st = stats[i];
    if (st) countUp(node, st.raw, st.fmt);
  });
}

export default async function page() {
  rendered = false;
  await load(false);
  return heartbeat(() => load(true, true), 30000);
}
