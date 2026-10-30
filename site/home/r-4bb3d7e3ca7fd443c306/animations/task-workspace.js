(() => {
  'use strict';
  const root = document.documentElement, workspace = document.querySelector('.workspace');
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  const stages = ['context', 'build', 'test', 'done'];
  const lines = ['const context = await collect({', '  sources: ["需求", "代码", "约束"],', '  boundary: "authorized"', '});', '', 'const change = await implement(context);', 'await verify(change, ["routes", "usage"]);', 'return receipt(change);'];
  const footers = ['正在整理上下文', '实现正在展开，约束保持可见', '逐项验证接口、路径与回执', '实现与验证完成，回到下一次想法'];
  const contexts = ['需求边界已关联', '当前实现持续同步', '验证约束跟随变更'];
  let paused = parent !== window, override = false, timer = 0, step = 0;
  let previousHeight = 0;
  function reportHeight() {
    if (parent === window) return;
    const style = getComputedStyle(document.body);
    const height = Math.ceil(workspace.getBoundingClientRect().height + parseFloat(style.paddingTop || 0) + parseFloat(style.paddingBottom || 0) + 2);
    if (height > 0 && height !== previousHeight) {
      previousHeight = height; parent.postMessage({type:'task-height',height}, '*');
    }
  }
  const text = (id, value) => { document.getElementById(id).textContent = value; };
  function render() {
    // Deterministic half-second beats; pausing preserves the exact current frame.
    const beat = step % 26, stage = beat < 6 ? 0 : beat < 15 ? 1 : beat < 22 ? 2 : 3;
    workspace.dataset.stage = stages[stage];
    const count = Math.min(3, Math.floor(beat / 2) + 1);
    document.querySelectorAll('[data-context]').forEach((card, i) => {
      card.classList.toggle('arrived', i < count);
      card.classList.toggle('focused', i === Math.floor(beat / 2) % count);
    });
    document.querySelectorAll('[data-nav]').forEach(node => node.classList.toggle('selected', node.dataset.nav === stages[stage]));
    document.querySelectorAll('[data-step]').forEach((node, i) => {
      node.classList.toggle('running', i === stage); node.classList.toggle('done', i < stage);
      node.querySelector('.status').textContent = i < stage ? '已完成' : i === stage ? '进行中' : '等待';
    });
    const code = lines.join('\n'), chars = stage === 0 ? 0 : Math.min(code.length, Math.floor((beat - 5) / 10 * code.length));
    document.querySelector('#codeStream code').textContent = code.slice(0, chars);
    document.querySelectorAll('[data-check]').forEach((node, i) => {
      const done = beat >= 16 + i * 2; node.classList.toggle('checked', done);
      node.querySelector('span').textContent = done ? '✓ 通过' : stage === 2 && i === Math.floor((beat - 15) / 2) ? '核对中' : '等待';
    });
    const percent = Math.min(100, Math.round((beat + 1) / 23 * 100));
    text('stageCounter', `0${stage + 1} / 04`); text('demoThroughput', `0${stage + 1} / 04`);
    text('contextCount', count); text('demoProgress', percent); text('taskFooter', footers[stage]);
    text('contextSummary', count === 3 ? contexts[Math.floor(beat / 2) % 3] : '关联需求、代码与约束');
    text('codeCaption', stage < 2 ? '从上下文到实现' : stage === 2 ? '验证工作流' : '生成可回看的回执');
    document.getElementById('taskProgress').style.width = percent + '%';
    document.getElementById('deliveryReceipt').classList.toggle('visible', stage === 3);
    document.querySelectorAll('.bars i').forEach((bar, i) => { bar.style.height = `${22 + ((beat * 11 + i * 17) % 65)}%`; });
  }
  function sync() {
    clearInterval(timer); timer = 0;
    const running = !paused && (!media.matches || override) && !document.hidden;
    root.toggleAttribute('data-motion-override', override); root.toggleAttribute('data-scene-paused', !running);
    root.dataset.playback = running ? 'playing' : 'paused';
    if (running) timer = setInterval(() => { step++; render(); }, 500);
  }
  addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'task-playback' || typeof event.data.paused !== 'boolean') return;
    paused = event.data.paused; override = event.data.reducedMotionOverride === true; sync();
  });
  media.addEventListener('change', sync); document.addEventListener('visibilitychange', sync);
  addEventListener('pagehide', () => { paused = true; sync(); });
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(reportHeight).observe(workspace);
  addEventListener('resize',reportHeight); addEventListener('load',reportHeight);
  document.fonts?.ready.then(reportHeight);
  render(); sync(); reportHeight(); if (parent !== window) parent.postMessage({type: 'task-ready'}, '*');
})();
