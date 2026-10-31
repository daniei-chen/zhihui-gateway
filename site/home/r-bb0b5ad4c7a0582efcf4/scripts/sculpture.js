const gallery = document.getElementById('thinkerGallery');
if (gallery) {
  const root = document.documentElement;
  const host = document.getElementById('thinkerViewport');
  const status = document.getElementById('thinkerStatus');
  const motion = document.getElementById('thinkerMotion');
  const reset = document.getElementById('thinkerReset');
  const views = [...gallery.querySelectorAll('[data-sculpture-view]')];
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  let viewer, loading = false, paused = false, visible = false, active = true, failed = false;
  const blocked = () => paused || root.hasAttribute('data-home-flow-paused') || root.hasAttribute('data-reduced-motion') || (media.matches && !root.hasAttribute('data-motion-override'));
  function sync() {
    const stop = blocked();
    motion.textContent = stop ? '自动转动' : '暂停转动';
    motion.setAttribute('aria-pressed', String(stop));
    viewer?.setPlayback(!stop && active && !document.hidden, visible);
  }
  function fail() {
    failed = true; gallery.dataset.state = 'error';
    status.textContent = '暂时无法开启三维视图，已显示雕塑静态渲染。';
    gallery.querySelectorAll('button').forEach(button => { button.disabled = true; });
    viewer?.setPlayback(false, false);
  }
  async function load() {
    if (loading || viewer || failed) return;
    loading = true; gallery.dataset.state = 'loading';
    status.textContent = '正在加载雕塑细节…';
    try {
      const { createSculptureViewer } = await import('/home-assets/r-bb0b5ad4c7a0582efcf4/scripts/sculpture-viewer.js');
      viewer = await createSculptureViewer(host, () => { paused = true; sync(); });
      if (failed) { viewer.dispose(); return; }
      gallery.dataset.state = 'ready';
      gallery.querySelectorAll('button').forEach(button => { button.disabled = false; });
      sync();
    } catch { fail(); }
    finally { loading = false; }
  }
  motion.addEventListener('click', () => {
    if (blocked()) { paused = false; document.dispatchEvent(new CustomEvent('zhihui:motion-request', { detail: { play: true } })); }
    else paused = true;
    sync();
  });
  function selectView(value) {
    paused = true;
    views.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.sculptureView === value)));
    viewer?.setView(value); sync();
  }
  views.forEach(button => button.addEventListener('click', () => selectView(button.dataset.sculptureView)));
  reset.addEventListener('click', () => selectView('full'));
  host.addEventListener('sculpture:error', fail);
  host.addEventListener('sculpture:view', event => views.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.sculptureView === event.detail))));
  document.addEventListener('zhihui:motion-change', sync);
  document.addEventListener('visibilitychange', sync);
  media.addEventListener('change', sync);
  addEventListener('pagehide', () => { active = false; sync(); });
  addEventListener('pageshow', () => { active = true; sync(); });
  if ('IntersectionObserver' in window) {
    const preload = new IntersectionObserver(entries => { if (entries[0].isIntersecting) { load(); preload.disconnect(); } }, { rootMargin: '400px' });
    preload.observe(gallery);
    new IntersectionObserver(entries => { visible = entries[0].isIntersecting; sync(); }, { threshold: 0 }).observe(host);
  } else { visible = true; load(); }
  sync();
}
