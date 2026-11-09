const stage = document.getElementById('gatewayStage');
if (stage) {
  const host = document.getElementById('gatewayCanvas'), button = document.getElementById('gatewayMotion');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let viewer, paused = false, override = false, visible = true, active = true, failed = false, state = 'idle';
  function sync() {
    const blocked = paused || (reduced.matches && !override);
    button.setAttribute('aria-pressed',String(blocked)); button.textContent = blocked ? '开启动效' : '暂停动效';
    viewer?.setPlayback(!blocked && active && !document.hidden,visible);
  }
  function fail() { failed = true; stage.dataset.state = 'poster'; button.disabled = true; viewer?.dispose(); }
  button.addEventListener('click',() => { if (paused || (reduced.matches && !override)) { paused=false; override=true; } else paused=true; sync(); });
  document.addEventListener('zhihui:login-state',event => { state=event.detail; viewer?.setState(state); });
  document.addEventListener('visibilitychange',sync);
  reduced.addEventListener('change',() => { override=false; sync(); });
  addEventListener('pagehide',() => { active=false; sync(); }); addEventListener('pageshow',() => { active=true; sync(); });
  host.addEventListener('gateway:error',fail);
  if ('IntersectionObserver' in window) new IntersectionObserver(entries => { visible=entries[0].isIntersecting; sync(); }).observe(host);
  async function load() {
    try {
      const { createGatewayViewer } = await import('/home-assets/r-af83712c3a6f09f1948e/scripts/gateway-viewer.js');
      viewer = createGatewayViewer(host);
      if (failed) { viewer.dispose(); return; }
      viewer.setState(state); stage.dataset.state='ready'; button.disabled=false; sync();
    } catch { fail(); }
  }
  sync();
  // Keep the form available before initializing the decorative renderer.
  if ('requestIdleCallback' in window) requestIdleCallback(load,{timeout:1500}); else setTimeout(load,350);
}
