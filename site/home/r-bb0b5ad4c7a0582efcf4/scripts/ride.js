/* Sandboxed illustrations share one explicit playback contract. */
(() => {
  'use strict';
  const root = document.documentElement;
  for (const config of [
    { frame: 'pelicanRide', button: 'rideMotionControl', channel: 'pelican', play: '播放骑行', pause: '暂停骑行' },
    { frame: 'taskWorkspace', button: 'taskMotionControl', channel: 'task', play: '播放演示', pause: '暂停演示' },
  ]) {
    const frame = document.getElementById(config.frame), button = document.getElementById(config.button);
    if (!frame || !button) continue;
    let visible = true, paused = false, pageActive = true;
    function sync() {
      const blocked = paused || root.hasAttribute('data-reduced-motion') || root.hasAttribute('data-home-flow-paused');
      const playing = !blocked && visible && pageActive && !document.hidden;
      // A scripts-only iframe sandbox has an opaque origin; no user data is sent.
      frame.contentWindow?.postMessage({ type: `${config.channel}-playback`, paused: !playing, reducedMotionOverride: root.hasAttribute('data-motion-override') }, '*');
      frame.dataset.playback = playing ? 'playing' : 'paused';
      button.textContent = blocked ? config.play : config.pause;
      button.setAttribute('aria-pressed', String(blocked));
    }
    button.addEventListener('click', () => {
      if (paused || root.hasAttribute('data-reduced-motion') || root.hasAttribute('data-home-flow-paused')) {
        paused = false;
        document.dispatchEvent(new CustomEvent('zhihui:motion-request', { detail: { play: true } }));
      } else paused = true;
      sync();
    });
    frame.addEventListener('load', sync);
    addEventListener('message', event => {
      if (event.source !== frame.contentWindow) return;
      if (config.channel === 'task' && event.data?.type === 'task-height') {
        const height = event.data.height;
        if (Number.isInteger(height) && height >= 240 && height <= 1600) {
          frame.parentElement.style.height = `${height}px`;
          frame.dataset.contentHeight = String(height);
        }
        return;
      }
      if (event.data?.type === `${config.channel}-ready`) sync();
      if (event.data?.type === `${config.channel}-motion-request`) {
        paused = false;
        document.dispatchEvent(new CustomEvent('zhihui:motion-request', { detail: { play: true } }));
        sync();
      }
    });
    document.addEventListener('zhihui:motion-change', sync);
    document.addEventListener('visibilitychange', sync);
    addEventListener('pagehide', () => { pageActive = false; sync(); });
    addEventListener('pageshow', () => { pageActive = true; sync(); });
    if ('IntersectionObserver' in window) new IntersectionObserver(entries => { visible = entries[0].isIntersecting; sync(); }, { threshold: 0 }).observe(frame);
    sync();
  }
})();
