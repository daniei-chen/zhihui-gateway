/* UI 组件库：轻量 DOM 构建 + 安全转义 + 通用组件。
   刻意不用 innerHTML 拼接业务数据，所有动态文本走 textContent 或 esc()。 */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat(4)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const frag = (...children) => {
  const f = document.createDocumentFragment();
  for (const c of children.flat(4)) if (c !== null && c !== undefined && c !== false) f.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return f;
};

export const num = (v, digits = 0) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
};
export const fmtTokens = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'k';
  return num(n);
};
export const pct = (v, d = 1) => (Number.isFinite(Number(v)) ? Number(v).toFixed(d) + '%' : '—');
export const rel = (iso) => {
  if (!iso) return '—';
  const t = Date.parse(String(iso).replace(' ', 'T'));
  if (!Number.isFinite(t)) return String(iso);
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(t).toLocaleDateString('zh-CN');
};

/* ---------------- 基础组件 ---------------- */
export function badge(text, kind = '') {
  return el('span', { class: `badge ${kind}`.trim(), text: String(text ?? '') });
}

export function statCard({ label, value, sub = '', icon = 'dot', tone = '' }) {
  return el('div', { class: 'stat' },
    el('div', { class: 'stat-top' },
      el('div', { class: 'stat-label', text: label }),
      el('div', { class: 'stat-icon', html: ICONS[icon] || ICONS.dot }),
    ),
    el('div', {},
      el('div', { class: `stat-value ${tone}`.trim(), text: value }),
      sub ? el('div', { class: 'stat-sub', text: sub }) : null,
    ),
  );
}

export function card(title, body, { actions = null, sub = '', plain = false } = {}) {
  return el('section', { class: `card${plain ? ' plain' : ''}` },
    title || sub || actions
      ? el('div', { class: 'card-head' },
          el('div', {}, el('h3', { text: title }), sub ? el('span', { class: 'muted', text: sub }) : null),
          actions ? el('div', { class: 'row' }, ...(Array.isArray(actions) ? actions : [actions])) : null,
        )
      : null,
    body,
  );
}

export function kv(k, v, { tone = '' } = {}) {
  return el('div', { class: 'kv' },
    el('span', { class: 'k', html: ICONS.dot, text: k }),
    el('span', { class: `v ${tone}`.trim(), text: v }),
  );
}

export function table(columns, rows, { empty = '暂无数据', maxHeight = '' } = {}) {
  const thead = el('thead', {}, el('tr', {},
    ...columns.map((c) => el('th', { class: c.num ? 'num' : '', text: c.title })),
  ));
  const tbody = el('tbody', {});
  if (!rows.length) {
    tbody.append(el('tr', {}, el('td', { class: 'empty', colspan: String(columns.length), text: empty })));
  } else {
    for (const row of rows) {
      const tr = el('tr', {});
      for (const c of columns) {
        const td = el('td', { class: c.num ? 'num' : '' });
        const v = c.render ? c.render(row) : row[c.key];
        if (v instanceof Node) td.append(v);
        else td.textContent = v ?? '—';
        tr.append(td);
      }
      tbody.append(tr);
    }
  }
  const wrap = el('div', { class: 'table-wrap' }, el('table', {}, thead, tbody));
  if (maxHeight) wrap.style.maxHeight = maxHeight;
  return wrap;
}

export function skeletonCards(n = 5) {
  return el('div', { class: 'grid stats' }, ...Array.from({ length: n }, () => el('div', { class: 'skeleton skel-card' })));
}

export function skeletonBlock(h = 160) {
  const d = el('div', { class: 'skeleton' });
  d.style.height = h + 'px';
  return d;
}

/* ---------------- Toast ---------------- */
export function toast(title, detail = '', kind = 'ok', ttl = 3600) {
  const root = $('#toasts');
  if (!root) return;
  const node = el('div', { class: `toast ${kind}`, role: 'status', style: `--toast-ttl:${ttl}ms;animation:none` },
    el('strong', { text: title }),
    detail ? el('span', { class: 'muted', text: detail }) : null,
  );
  root.append(node);
  const m = M();
  if (m) m.animate(node, { opacity: [0, 1], x: [14, 0], scale: [0.98, 1] }, { duration: 0.32, type: 'spring', stiffness: 380, damping: 30 });
  while (root.children.length > 4) root.firstElementChild.remove();
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .2s';
    setTimeout(() => node.remove(), 240);
  }, ttl);
}

/* ---------------- Modal ---------------- */
let closeCurrent = null;

export function modal({ title, body, actions = [], onClose = null, wide = false }) {
  closeModal();
  const mask = el('div', { class: 'modal-mask', role: 'presentation' });
  const box = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title || '对话框' });
  if (wide) box.style.width = 'min(720px, 100%)';
  box.append(el('h3', { text: title || '' }));
  box.append(body instanceof Node ? body : el('div', { html: String(body || '') }));
  const footer = el('div', { class: 'modal-actions' });
  for (const a of actions) {
    footer.append(el('button', {
      class: `btn ${a.kind || ''}`.trim(), type: 'button', text: a.label,
      onclick: async () => {
        if (a.onClick) {
          const keep = await a.onClick();
          if (keep === true) return;
        }
        closeModal();
      },
    }));
  }
  if (actions.length) box.append(footer);
  mask.append(box);
  mask.addEventListener('mousedown', (e) => { if (e.target === mask) closeModal(); });
  const onKey = (e) => { if (e.key === 'Escape') closeModal(); };
  document.addEventListener('keydown', onKey);
  closeCurrent = () => {
    document.removeEventListener('keydown', onKey);
    mask.remove();
    closeCurrent = null;
    if (onClose) onClose();
  };
  $('#modalRoot').append(mask);
  animateModal(box, mask);
  const focusable = box.querySelector('input,textarea,select,button');
  if (focusable) setTimeout(() => focusable.focus(), 30);
  return mask;
}

export function closeModal() { if (closeCurrent) closeCurrent(); }

export function confirmDialog(title, message, { danger = false, confirmText = '确认' } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    modal({
      title,
      body: el('p', { class: 'muted', text: message }),
      onClose: () => { if (!settled) { settled = true; resolve(false); } },
      actions: [
        { label: '取消', onClick: () => { settled = true; resolve(false); } },
        {
          label: confirmText, kind: danger ? 'danger' : 'primary',
          onClick: () => { settled = true; resolve(true); },
        },
      ],
    });
  });
}

/* ---------------- 图标（内联 SVG，避免外部依赖） ---------------- */
export const ICONS = {
  dot: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg>',
  users: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 5.5a3.5 3.5 0 0 1 0 7M18 20a6 6 0 0 0-2-4.5"/></svg>',
  check: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 9v5M12 17h.01"/></svg>',
  key: '<svg viewBox="0 0 24 24"><circle cx="7.5" cy="15.5" r="3.5"/><path d="m10 13 9-9M15 4l3 3M18 7l2 2"/></svg>',
  chart: '<svg viewBox="0 0 24 24"><path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/></svg>',
  dash: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></svg>',
  account: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  usage: '<svg viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="m7 14 3-4 3 3 5-7"/></svg>',
  model: '<svg viewBox="0 0 24 24"><path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="m3 16 9 5 9-5"/><path d="m3 12 9 5 9-5"/></svg>',
  logs: '<svg viewBox="0 0 24 24"><path d="M4 5h16M4 12h16M4 19h10"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0-6 6c0 7-3 7-3 9h18c0-2-3-2-3-9a6 6 0 0 0-6-6Z"/><path d="M10 21h4"/></svg>',
  settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>',
  audit: '<svg viewBox="0 0 24 24"><path d="M9 3h6l4 4v14H5V3Z"/><path d="M9 12h6M9 16h6"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
  checkin: '<svg viewBox="0 0 24 24"><path d="M8 2v4M16 2v4M3 10h18"/><rect x="3" y="4" width="18" height="18" rx="3"/><path d="m9 15 2 2 4-4"/></svg>',
};

/* ---------------- 状态映射 ---------------- */
export const STATE_LABEL = {
  ready: ['正常', 'ok'], cooling: ['冷却中', 'warn'], disabled: ['已停用', 'bad'],
  low: ['低额度', 'warn'], expired: ['已过期', 'bad'], unknown: ['未知', ''],
};
export function stateBadge(state) {
  const [label, kind] = STATE_LABEL[state] || STATE_LABEL.unknown;
  return badge(label, kind);
}

/* ---------------- 页头 ---------------- */
export function setHead(title, sub = '', ...actions) {
  const t = $('#pageTitle');
  const s = $('#pageSub');
  const a = $('#pageActions');
  if (t) t.textContent = title;
  if (s) s.textContent = sub;
  if (a) {
    a.replaceChildren();
    for (const act of actions.flat(3)) if (act) a.append(act);
  }
}

export function pageContainer() {
  return $('#page');
}

/* ------------------------------------------------------------------
   动效：直接使用 motion（WorkBuddy 用的同一个库 motion@12），
   参数照抄其源码：
     StatCard        initial={opacity:0,y:12} transition={duration:.35, delay}
     counting-number transition={stiffness:90, damping:50}（spring）
     floating-dock   距离感应 + useSpring
     motion-highlight layout 位移动画
   motion 以 UMD 形式加载到 window.Motion；未加载时全部降级为无动画。
   ------------------------------------------------------------------ */
const M = () => globalThis.Motion || null;

/** 元素入场：等价于 motion 的 initial/animate */
export function animateIn(node, { y = 12, duration = 0.35, delay = 0 } = {}) {
  const m = M();
  if (!m || !node) return;
  m.animate(node, { opacity: [0, 1], y: [y, 0] }, { duration, delay });
}

/** 容器内子元素逐个入场（staggered），对应 WorkBuddy 的 delay +0.05 */
export function animateChildren(root, { selector = ':scope > *', step = 0.05, y = 12, duration = 0.35 } = {}) {
  const m = M();
  if (!m || !root) return;
  const items = [...root.children].filter((n) => n.nodeType === 1);
  items.forEach((node, i) => m.animate(node, { opacity: [0, 1], y: [y, 0] }, { duration, delay: i * step }));
}

/** 数字滚动：等价于 counting-number 的 useSpring + useInView */
export function countUp(node, target, formatter = (n) => String(Math.round(n)), { inView = true } = {}) {
  const m = M();
  const to = Number(target);
  if (!node) return;
  if (!m || !Number.isFinite(to)) { node.textContent = formatter(target); return; }
  const run = () => {
    const mv = m.motionValue(0);
    mv.on('change', (v) => { node.textContent = formatter(v); });
    m.animate(mv, to, { type: 'spring', stiffness: 90, damping: 50 });
  };
  if (inView) m.inView(node, run, { amount: 0.2 });
  else run();
}

/** dock 距离感应：对齐 floating-dock 的 useMotionValue/useSpring/useTransform */
export function attachDockMagnify(dock, maxScale = 1.22, radius = 90) {
  const m = M();
  if (!dock || dock.dataset.magnify === '1') return;
  dock.dataset.magnify = '1';
  const reset = () => dock.querySelectorAll('.dock-btn').forEach((b) => { b.style.transform = ''; });
  dock.addEventListener('pointermove', (ev) => {
    const btns = dock.querySelectorAll('.dock-btn');
    for (const b of btns) {
      const r = b.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const dist = Math.abs(ev.clientX - cx);
      const scale = dist >= radius ? 1 : 1 + (maxScale - 1) * (1 - dist / radius);
      const lift = dist >= radius ? 0 : -3 * (1 - dist / radius);
      b.style.transform = `translateY(${lift.toFixed(2)}px) scale(${scale.toFixed(3)})`;
    }
  });
  dock.addEventListener('pointerleave', reset);
  dock.addEventListener('pointercancel', reset);
}

/** Tabs 指示器：对齐 motion-highlight 的 layout 位移 */
export function attachTabIndicators(root = document) {
  root.querySelectorAll('.tabs').forEach((tabs) => {
    if (tabs.querySelector('.tab-indicator')) return;
    const ind = el('span', { class: 'tab-indicator', 'aria-hidden': 'true' });
    tabs.prepend(ind);
    const place = (animateNow = true) => {
      const active = tabs.querySelector('.tab[aria-selected="true"]');
      if (!active) return;
      const tr = tabs.getBoundingClientRect();
      const ar = active.getBoundingClientRect();
      const x = ar.left - tr.left;
      const m = M();
      if (m && animateNow) {
        m.animate(ind, { x, width: ar.width }, { type: 'spring', stiffness: 320, damping: 34 });
      } else {
        ind.style.width = `${ar.width}px`;
        ind.style.transform = `translateX(${x}px)`;
      }
    };
    place(false);
    requestAnimationFrame(() => place(true));
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => place(false)).observe(tabs);
  });
}

/** 模态：对齐 animate-ui/radix/dialog 的 zoom 入场 */
export function animateModal(box, overlay) {
  const m = M();
  if (!m) return;
  if (overlay) m.animate(overlay, { opacity: [0, 1] }, { duration: 0.16 });
  if (box) m.animate(box, { opacity: [0, 1], scale: [0.96, 1], y: [6, 0] }, { duration: 0.24, type: 'spring', stiffness: 320, damping: 30 });
}

export function renderPage(...nodes) {
  const root = pageContainer();
  if (!root) return;
  root.replaceChildren(...nodes.flat(4).filter((n) => n !== null && n !== undefined && n !== false));
  // 重新触发入场动效：强制回流后再加 class，保证每次切换都重播
  // 页面入场（motion）+ 统计卡 stagger + tabs 指示器
  animateIn(root, { y: 8, duration: 0.28 });
  const statsGrid = root.querySelector('.grid.stats');
  if (statsGrid) {
    [...statsGrid.children].forEach((node, i) => animateIn(node, { y: 12, duration: 0.34, delay: 0.02 + i * 0.05 }));
  }
  attachTabIndicators(root);
}

export function errorBox(err, retry) {
  return el('div', { class: 'card' },
    el('div', { class: 'row between' },
      el('div', {},
        el('h3', { text: '加载失败' }),
        el('p', { class: 'muted sm', text: err?.message || String(err) || '未知错误' }),
      ),
      retry ? el('button', { class: 'btn sm', type: 'button', text: '重试', onclick: retry }) : null,
    ),
  );
}

export function loadingRows(n = 3) {
  return el('div', { class: 'stack' }, ...Array.from({ length: n }, () => {
    const d = el('div', { class: 'skeleton' });
    d.style.height = '64px';
    return d;
  }));
}

/* ---------------- 心跳刷新 ----------------
   借鉴 WorkBuddy 的 useHeartbeat：页面隐藏时跳过，切回标签页立即补刷。
   与「重建 DOM 的定时轮询」相比，它只在数据变化时局部更新，避免焦点丢失。 */
export function heartbeat(fn, ms = 30000) {
  let timer = null;
  let running = false;
  const tick = async () => {
    if (document.hidden || running) return;
    running = true;
    try { await fn(); } catch { /* 心跳失败静默，由页面自己决定是否提示 */ }
    finally { running = false; }
  };
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  const onVisible = () => { if (!document.hidden) tick(); };
  timer = setInterval(tick, ms);
  document.addEventListener('visibilitychange', onVisible);
  return () => { stop(); document.removeEventListener('visibilitychange', onVisible); };
}
