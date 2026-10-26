/* SVG 图表：单调三次曲线（与 WorkBuddy 的 recharts curveMonotoneX 观感一致）。
   零依赖、可打印、跟随 CSS 变量换色。 */

const NS = 'http://www.w3.org/2000/svg';

function s(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  }
  return node;
}

/** 单调三次插值控制点（Fritsch–Carlson / d3 curveMonotoneX） */
export function monotonePath(pts) {
  const n = pts.length;
  if (n === 0) return '';
  if (n === 1) return `M${pts[0][0]},${pts[0][1]}`;
  if (n === 2) return `M${pts[0][0]},${pts[0][1]} L${pts[1][0]},${pts[1][1]}`;
  const dx = [], dy = [], m = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    dy[i] = pts[i + 1][1] - pts[i][1];
    m[i] = dx[i] === 0 ? 0 : dy[i] / dx[i];
  }
  const t = new Array(n);
  t[0] = m[0];
  t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (m[i - 1] * m[i] <= 0) t[i] = 0;
    else {
      const w1 = 2 * dx[i] + dx[i - 1];
      const w2 = dx[i] + 2 * dx[i - 1];
      t[i] = (w1 + w2) / (w1 / m[i - 1] + w2 / m[i]);
    }
  }
  let d = `M${pts[0][0].toFixed(2)},${pts[0][1].toFixed(2)}`;
  for (let i = 0; i < n - 1; i++) {
    const x0 = pts[i][0], y0 = pts[i][1];
    const x1 = pts[i + 1][0], y1 = pts[i + 1][1];
    const h = dx[i];
    d += ` C${(x0 + h / 3).toFixed(2)},${(y0 + t[i] * h / 3).toFixed(2)}`
       + ` ${(x1 - h / 3).toFixed(2)},${(y1 - t[i + 1] * h / 3).toFixed(2)}`
       + ` ${x1.toFixed(2)},${y1.toFixed(2)}`;
  }
  return d;
}

function niceMax(v) {
  if (!Number.isFinite(v) || v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const f = v / base;
  const step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return step * base;
}

function fmtAxis(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '';
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n * 100) / 100);
}

/**
 * @param {object} opts
 *  labels: string[]
 *  series: [{name, values:number[], cls:'series-1'|'series-2'|'series-bad', area?:bool}]
 */
export function lineChart({ labels = [], series = [], height = 200, padL = 38, padR = 12, padT = 12, padB = 22 } = {}) {
  const width = 720;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const n = Math.max(labels.length, 1);
  const all = series.flatMap((s0) => s0.values.filter((v) => Number.isFinite(v)));
  const max = niceMax(Math.max(1, ...all));
  const xAt = (i) => padL + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const yAt = (v) => padT + innerH - (Math.max(0, Number(v) || 0) / max) * innerH;

  const svg = s('svg', {
    class: 'chart', viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none',
    role: 'img', 'aria-label': series.map((x) => x.name).join('、') + ' 趋势图',
  });

  // 网格 + y 轴刻度
  for (let i = 0; i <= 4; i++) {
    const y = padT + (innerH * i) / 4;
    svg.append(s('line', { class: 'grid-line', x1: padL, y1: y, x2: width - padR, y2: y }));
    const label = s('text', { class: 'axis-text', x: padL - 6, y: y + 3, 'text-anchor': 'end' });
    label.textContent = fmtAxis(max * (1 - i / 4));
    svg.append(label);
  }

  // x 轴标签（最多 7 个，避免拥挤）
  const step = Math.max(1, Math.ceil(n / 7));
  labels.forEach((lb, i) => {
    if (i % step !== 0 && i !== n - 1) return;
    const t = s('text', { class: 'axis-text', x: xAt(i), y: height - 6, 'text-anchor': 'middle' });
    t.textContent = lb;
    svg.append(t);
  });

  // 序列
  for (const ser of series) {
    const pts = ser.values.map((v, i) => [xAt(i), yAt(v)]);
    if (!pts.length) continue;
    const d = monotonePath(pts);
    if (ser.area) {
      const areaD = `${d} L${xAt(pts.length - 1).toFixed(2)},${(padT + innerH).toFixed(2)} L${xAt(0).toFixed(2)},${(padT + innerH).toFixed(2)} Z`;
      svg.append(s('path', { class: 'area-1', d: areaD, pathLength: 1 }));
    }
    svg.append(s('path', { class: ser.cls || 'series-1', d, pathLength: 1 }));
  }
  return svg;
}

/** 双序列（收入/支出）折线 */
export function dualLine({ labels = [], a = [], b = [], aName = '收入', bName = '支出', height = 190 } = {}) {
  return lineChart({
    labels, height,
    series: [
      { name: aName, values: a, cls: 'series-2' },
      { name: bName, values: b, cls: 'series-bad' },
    ],
  });
}

export function legend(items) {
  const wrap = document.createElement('div');
  wrap.className = 'legend';
  for (const it of items) {
    const span = document.createElement('span');
    const dot = document.createElement('i');
    dot.style.background = it.color;
    span.append(dot, document.createTextNode(it.name));
    wrap.append(span);
  }
  return wrap;
}
