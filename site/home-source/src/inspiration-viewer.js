import {createInspirationRenderer} from './inspiration-renderer.js';

export const FPS = 24;
export function startInspirationViewer({win = window, doc = document, rendererFactory = createInspirationRenderer} = {}) {
  const surface = doc.getElementById('inspirationSurface');
  const canvas = doc.getElementById('inspirationCanvas');
  const status = doc.getElementById('inspirationStatus');
  const media = win.matchMedia('(prefers-reduced-motion: reduce)');
  const standalone = win.parent === win;
  const listeners = [], observers = [];
  let renderer, disposed = false, failed = false, ready = false, loading = false;
  let paused = !standalone, override = false, visible = true, pageActive = true;
  let frame = 0, last = null, elapsed = 18000, mode = 'flow';
  let pointer = [0,0], target = [0,0];
  const on = (object, name, callback, options) => {
    object.addEventListener(name, callback, options);
    listeners.push(() => object.removeEventListener(name, callback, options));
  };
  const allowed = () => !disposed && !failed && !paused && visible && pageActive && !doc.hidden && (!media.matches || override);
  function stop() { if (frame) win.cancelAnimationFrame(frame); frame = 0; last = null; }
  function fail() {
    failed = true; ready = false; stop(); renderer?.dispose();
    doc.documentElement.dataset.scene = 'fallback';
    status.textContent = '静态灵感画面 · 当前设备未启用动态渲染';
    doc.querySelectorAll('[data-flow-mode]').forEach(button => { button.disabled = true; });
  }
  function render() {
    try {
      renderer.render({width: surface.clientWidth, height: surface.clientHeight,
        time: elapsed / 1000 * .22, pointer, mode});
      doc.documentElement.dataset.scene = 'ready';
    } catch { fail(); }
  }
  function tick(now) {
    frame = 0;
    if (!allowed() || !ready) return;
    if (last === null || now - last >= 1000 / FPS) {
      const delta = last === null ? 0 : Math.min(100, now - last);
      elapsed += delta; last = now;
      pointer = pointer.map((value, index) => value + (target[index] - value) * .08);
      render();
    }
    if (allowed()) frame = win.requestAnimationFrame(tick);
  }
  async function initialize() {
    if (loading || ready || disposed || failed) return;
    loading = true;
    try {
      renderer = rendererFactory(canvas);
      await renderer.ready;
      if (disposed || failed) return;
      ready = true;
      status.textContent = '移动指针，轻轻拨动灵感';
      doc.querySelectorAll('[data-flow-mode]').forEach(button => { button.disabled = false; });
      sync();
    } catch { if (!disposed) fail(); }
    finally { loading = false; }
  }
  function sync() {
    if (!allowed()) { stop(); return; }
    if (!ready) { void initialize(); return; }
    if (!frame) frame = win.requestAnimationFrame(tick);
  }
  on(win, 'message', event => {
    if (event.source !== win.parent || event.data?.type !== 'pelican-playback' || typeof event.data.paused !== 'boolean') return;
    paused = event.data.paused;
    override = event.data.reducedMotionOverride === true;
    sync();
  });
  on(doc, 'visibilitychange', sync);
  on(media, 'change', sync);
  on(win, 'pagehide', event => { pageActive = false; stop(); if (!event.persisted) dispose(); });
  on(win, 'pageshow', () => { pageActive = true; sync(); });
  on(canvas, 'webglcontextlost', event => { event.preventDefault(); fail(); });
  on(surface, 'pointermove', event => {
    if (event.pointerType === 'touch' || !allowed()) return;
    const box = surface.getBoundingClientRect();
    if (!box.width || !box.height) return;
    target = [(Math.max(0, Math.min(1, (event.clientX-box.left)/box.width))-.5)*.055,
      (.5-Math.max(0, Math.min(1, (event.clientY-box.top)/box.height)))*.055];
  }, {passive:true});
  on(surface, 'pointerleave', () => { target = [0,0]; });
  for (const button of doc.querySelectorAll('[data-flow-mode]')) on(button, 'click', () => {
    if (!['flow','open','focus'].includes(button.dataset.flowMode) || !ready || failed) return;
    mode = button.dataset.flowMode;
    doc.querySelectorAll('[data-flow-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    if (!doc.hidden && visible && pageActive) render();
  });
  if (win.IntersectionObserver) {
    const observer = new win.IntersectionObserver(entries => { visible = entries[0]?.isIntersecting === true; sync(); });
    observer.observe(surface); observers.push(observer);
  }
  function dispose() {
    if (disposed) return;
    disposed = true; stop(); renderer?.dispose();
    observers.forEach(observer => observer.disconnect()); listeners.forEach(remove => remove());
  }
  win.parent.postMessage({type:'pelican-ready'}, '*');
  sync();
  return {dispose};
}

if (typeof window !== 'undefined' && document.getElementById('inspirationSurface')) startInspirationViewer();
