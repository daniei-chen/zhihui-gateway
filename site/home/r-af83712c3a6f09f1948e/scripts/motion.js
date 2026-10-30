/* Original homepage flow, refined with one lifecycle and bounded pointer motion. */
(() => {
  'use strict';
  const root = document.documentElement;
  const canvas = document.getElementById('zh-intelligence-canvas');
  const host = canvas?.parentElement;
  const context = canvas?.getContext('2d', { alpha: true });
  if (!host || !context) return;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const fine = matchMedia('(hover:hover) and (pointer:fine)');
  let width = 0, height = 0, frame = 0, last = 0, elapsed = 0, visible = true, pageActive = true;
  let points = [], slowFrames = 0;
  let pointer = { active: false, x: 0, y: 0 };
  const reduced = () => reduce.matches && !root.hasAttribute('data-motion-override');
  const live = () => pageActive && visible && !document.hidden && !reduced() && !root.hasAttribute('data-home-flow-paused');
  const random = (index, salt) => { const value = Math.sin(index * 127.1 + salt * 311.7) * 43758.5453; return value - Math.floor(value); };
  function render(dt) {
    context.clearRect(0, 0, width, height);
    const phase = elapsed * .00008;
    for (const point of points) {
      point.u = (point.u + dt * point.speed / Math.max(width, 1)) % 1;
      const x = point.u * (width + 120) - 60;
      const y = point.lane * height + Math.sin(point.u * 6 + point.seed * 5 + phase) * height * .065;
      let px = x, py = y;
      if (pointer.active) {
        const dx = pointer.x - x, dy = pointer.y - y;
        const pull = Math.max(0, 1 - Math.hypot(dx, dy) / 190) * .1;
        px += dx * pull; py += dy * pull;
      }
      const quiet = y < 420 ? Math.max(.1, Math.min(1, Math.abs(x - width / 2) / (width * .34))) : .5;
      const edge = Math.max(0, Math.min(1, (x + 50) / 80, (width + 50 - x) / 80));
      const alpha = (.24 + point.seed * .25) * quiet * edge;
      context.beginPath(); context.strokeStyle = `rgba(84,133,231,${alpha * .55})`; context.lineWidth = .7;
      context.moveTo(px - 4 - point.seed * 10, py - 1); context.lineTo(px, py); context.stroke();
      context.beginPath(); context.fillStyle = `rgba(76,125,224,${alpha})`;
      context.arc(px, py, point.radius, 0, Math.PI * 2); context.fill();
    }
  }
  function stop() {
    cancelAnimationFrame(frame); frame = 0; last = 0;
    canvas.dataset.flowState = 'paused';
  }
  function tick(now) {
    frame = 0;
    if (!live()) { stop(); return; }
    if (last && now - last < 32) { frame = requestAnimationFrame(tick); return; }
    const dt = last ? Math.min(now - last, 65) : 32;
    last = now; elapsed += dt;
    const start = performance.now();
    render(dt);
    if (performance.now() - start > 8 && points.length > 45) {
      if (++slowFrames > 6) { points = points.slice(0, Math.floor(points.length * .75)); slowFrames = 0; }
    } else slowFrames = 0;
    canvas.dataset.flowState = 'running';
    frame = requestAnimationFrame(tick);
  }
  function sync() {
    stop();
    if (live()) frame = requestAnimationFrame(tick);
    else { pointer.active = false; context.clearRect(0, 0, width, height); }
  }
  function resize() {
    const box = host.getBoundingClientRect();
    width = box.width; height = box.height;
    const ratio = Math.min(1.5, devicePixelRatio || 1);
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const count = width <= 760 ? 48 : Math.min(160, Math.max(75, Math.floor(width * height / 6500)));
    points = Array.from({ length: count }, (_, index) => ({ u: random(index, 1), lane: random(index, 2) * 1.1 - .05, seed: random(index, 3), speed: .022 + random(index, 4) * .023, radius: .6 + random(index, 5) * .6 }));
    canvas.dataset.particleCount = String(count);
    sync();
  }
  const hero = document.getElementById('top');
  hero?.addEventListener('pointermove', event => {
    if (!fine.matches || !live()) return;
    const box = host.getBoundingClientRect();
    pointer = { active: true, x: event.clientX - box.left, y: event.clientY - box.top };
  }, { passive: true });
  hero?.addEventListener('pointerleave', () => { pointer.active = false; });
  if ('ResizeObserver' in window) new ResizeObserver(resize).observe(host);
  else addEventListener('resize', resize, { passive: true });
  if ('IntersectionObserver' in window) new IntersectionObserver(entries => { visible = entries[0].isIntersecting; sync(); }).observe(host);
  document.addEventListener('zhihui:motion-change', sync);
  reduce.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  addEventListener('pagehide', () => { pageActive = false; sync(); });
  addEventListener('pageshow', () => { pageActive = true; sync(); });

  for (const node of document.querySelectorAll('.visual-wrap,.feature')) {
    const surface = node.querySelector('.demo-browser') || node;
    let pending = 0, latest;
    const reset = () => {
      cancelAnimationFrame(pending); pending = 0;
      for (const name of ['--stage-rx','--stage-ry','--stage-y','--pointer-x','--pointer-y','--pointer-active']) surface.style.removeProperty(name);
    };
    const allowed = () => fine.matches && !reduced() && !document.hidden && !root.hasAttribute('data-home-flow-paused');
    node.addEventListener('pointermove', event => {
      if (!allowed()) return;
      latest = { x: event.clientX, y: event.clientY };
      if (pending) return;
      pending = requestAnimationFrame(() => {
        pending = 0;
        if (!allowed()) { reset(); return; }
        const box = node.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (latest.x - box.left) / box.width));
        const y = Math.max(0, Math.min(1, (latest.y - box.top) / box.height));
        surface.style.setProperty('--pointer-x', `${x * 100}%`);
        surface.style.setProperty('--pointer-y', `${y * 100}%`);
        surface.style.setProperty('--pointer-active', '1');
        if (surface !== node) {
          surface.style.setProperty('--stage-rx', `${(.5 - y) * 1.6}deg`);
          surface.style.setProperty('--stage-ry', `${(x - .5) * 2.4}deg`);
          surface.style.setProperty('--stage-y', '-2px');
        }
      });
    }, { passive: true });
    node.addEventListener('pointerleave', reset);
    document.addEventListener('zhihui:motion-change', reset);
    document.addEventListener('visibilitychange', reset);
    fine.addEventListener('change', reset);
    reduce.addEventListener('change', reset);
  }
  resize();
})();
