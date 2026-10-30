/* Homepage interactions only: no API requests, credentials or business mutations. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const all = selector => [...document.querySelectorAll(selector)];
  const root = document.documentElement;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const motionButton = $('#zh-motion-control');
  let paused = false, motionOverride = false;
  const motionAllowed = () => !paused && (!reduce.matches || motionOverride) && !document.hidden;
  const isOnScreen = node => {
    if (!node) return false;
    const box = node.getBoundingClientRect();
    return box.bottom > 0 && box.top < innerHeight;
  };
  const transientTimers = new WeakMap();
  function pulse(node, name, duration = 700) {
    if (!node) return;
    clearTimeout(transientTimers.get(node));
    node.classList.remove(name);
    if (!motionAllowed() || !isOnScreen(node)) return;
    void node.offsetWidth;
    node.classList.add(name);
    transientTimers.set(node, setTimeout(() => node.classList.remove(name), duration));
  }

  const platforms = {
    workbuddy: { label: 'WorkBuddy', prefix: '/v1/*', engine: 'workbuddy2api', path: 'netA', text: 'WorkBuddy 专用入口。国内与国际版本独立授权。' },
    qoder: { label: 'Qoder', prefix: '/qoder/v1/*', engine: 'qoder2', path: 'netC', text: 'Qoder 专用入口，独立引擎与账号资源。' },
    trae: { label: 'Trae', prefix: '/trae/v1/*', engine: 'aiwork-server', path: 'netB', text: 'Trae 专用入口，独立账号池与调用记录。' },
  };
  let selected = 'workbuddy';
  let format = 'curl';
  const setText = (selector, text) => { const node = $(selector); if (node) node.textContent = text; };

  function codeFor() {
    const route = platforms[selected];
    const prefix = route.prefix.replace('/*', '');
    if (format === 'env') return `# ${route.label} · shell example (local placeholders only)\nexport OPENAI_BASE_URL="https://YOUR_GATEWAY_HOST${prefix}"\nexport OPENAI_API_KEY="YOUR_API_KEY"\n# 具体客户端的变量名及适配方式请以客户端文档为准`;
    if (format === 'python') return `# ${route.label} · Python OpenAI SDK example\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url="https://YOUR_GATEWAY_HOST${prefix}",\n    api_key="YOUR_API_KEY",\n)\nresponse = client.chat.completions.create(\n    model="YOUR_AUTHORIZED_MODEL",\n    messages=[{"role": "user", "content": "Hello!"}],\n)\nprint(response.choices[0].message.content)`;
    return `# ${route.label} · OpenAI-compatible example\ncurl --request POST \\\n  'https://YOUR_GATEWAY_HOST${prefix}/chat/completions' \\\n  --header 'Authorization: Bearer YOUR_API_KEY' \\\n  --header 'Content-Type: application/json' \\\n  --data '{\n    "model": "YOUR_AUTHORIZED_MODEL",\n    "messages": [{"role":"user","content":"Hello!"}]\n  }'`;
  }
  function renderCode(animate = true) {
    setText('#codeBlock', codeFor());
    setText('#codeFilename', format === 'python' ? `python · ${selected}-client.py` : format === 'env' ? `shell · ${selected}-env.sh` : `bash · ${selected}-request.sh`);
    if (animate) pulse($('.terminal'), 'is-code-changing', 250);
  }
  function choosePlatform(value, origin) {
    if (!platforms[value]) return;
    selected = value;
    const route = platforms[value];
    for (const [attribute, activeClass] of [['platform', 'active'], ['route', 'selected'], ['code', 'on'], ['hero-platform', 'is-selected']]) {
      all(`[data-${attribute}]`).forEach(node => {
        const on = node.getAttribute(`data-${attribute}`) === value;
        node.classList.toggle(activeClass, on);
        node.setAttribute('aria-pressed', String(on));
      });
    }
    setText('#platformName', route.label.toUpperCase());
    setText('#platformNode', route.label);
    setText('#platformPrefix', route.prefix);
    setText('#platformEngine', route.engine);
    setText('#platformText', route.text);
    setText('#zh3PlatformContext', `${route.label} · 光路与代码已同步`);
    setText('#routeLiveSummary', `当前展示：${route.label} · ${route.prefix}`);
    setText('#setupHint', '替换域名、Key 与授权模型后使用，实际配置以控制台为准。');
    all('[data-platform-target]').forEach(node => node.classList.toggle('is-current', node.dataset.platformTarget === value));
    all('[data-signal-route]').forEach(node => node.classList.toggle('is-selected', node.dataset.signalRoute === value));
    const diagram = $('.signal-diagram');
    if (diagram) diagram.dataset.selectedPlatform = value;
    pulse(diagram, 'is-selecting', 800);
    renderCode(origin !== 'init');
    resetRoute();
    if (origin === 'route') replayRoute();
    document.dispatchEvent(new CustomEvent('zhihui:platform-selected', { detail: { platform: value } }));
  }
  for (const attribute of ['platform', 'route', 'code', 'hero-platform']) {
    all(`[data-${attribute}]`).forEach(button => button.addEventListener('click', () => choosePlatform(button.getAttribute(`data-${attribute}`), attribute)));
  }
  window.setZhCodeFormat = value => {
    if (!['curl', 'python', 'env'].includes(value)) return;
    format = value;
    all('[data-format]').forEach(node => {
      const on = node.dataset.format === value;
      node.classList.toggle('on', on);
      node.setAttribute('aria-pressed', String(on));
    });
    renderCode();
  };
  all('[data-format]').forEach(button => button.addEventListener('click', () => window.setZhCodeFormat(button.dataset.format)));
  let toastTimer;
  function showToast(text) {
    const toast = $('#toast');
    if (!toast) return;
    toast.textContent = text;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
  }
  $('#copyCode')?.addEventListener('click', async () => {
    const value = codeFor();
    let temporary;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        temporary = document.createElement('textarea');
        temporary.value = value;
        temporary.setAttribute('readonly', '');
        temporary.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.append(temporary);
        temporary.select();
        if (!document.execCommand('copy')) throw new Error('copy unavailable');
      }
      showToast('配置示例已复制');
    } catch {
      showToast('无法自动复制，请手动选取代码');
    } finally {
      temporary?.remove();
      $('#copyCode')?.focus({ preventScroll: true });
    }
  });

  // One explicitly triggered request illustration; completed if motion is interrupted.
  const network = $('.network');
  const run = $('#zh23Run');
  const steps = all('.route-step');
  const tracer = $('#zh23Traveler');
  const halo = $('#zh23TravelerHalo');
  const trail = $('#zh23Trail');
  const result = $('.zh23-route-result');
  const scenes = {
    fresh: { steps: ['凭证检查', '账号筛选', '上游转发'], done: '路径演示完成 · 未调用任何真实接口' },
    sticky: { steps: ['识别会话', '沿用绑定', '上游转发'], done: '演示结束：原账号可用时优先沿用会话绑定' },
    fallback: { steps: ['检测不可用', '同平台重选', '转发或返回失败'], done: '演示结束：同平台备用路径仅在有可用账号时成立' },
  };
  let scenario = 'fresh', routeFrame = 0, routeRunning = false;
  function routeStatus(text, state) {
    setText('#zh23RouteStatus', text);
    if (result) result.dataset.state = state;
  }
  function clearRoute() {
    cancelAnimationFrame(routeFrame);
    routeFrame = 0;
    routeRunning = false;
    if (run) run.disabled = false;
    for (const node of [tracer, halo, trail]) if (node) node.style.opacity = '0';
    trail?.removeAttribute('stroke-dasharray');
    steps.forEach(node => node.classList.remove('zh23-is-active'));
    network?.classList.remove('zh23-done');
  }
  function resetRoute() {
    clearRoute();
    const route = platforms[selected];
    all('.network .lines').forEach(node => {
      node.classList.toggle('highlight', node.id === route.path);
      node.classList.toggle('dim', node.id !== route.path);
    });
    if (trail) trail.setAttribute('d', $(`#${route.path}`).getAttribute('d'));
    steps.forEach((node, index) => {
      const count = document.createElement('i');
      count.textContent = `0${index + 1}`;
      node.replaceChildren(count, document.createTextNode(' ' + scenes[scenario].steps[index]));
    });
    routeStatus(`已选择 ${route.label} · ${route.prefix}，可开始模拟`, 'idle');
  }
  function finishRoute() {
    clearRoute();
    steps[2]?.classList.add('zh23-is-active');
    network?.classList.add('zh23-done');
    routeStatus(scenes[scenario].done, 'done');
  }
  function replayRoute() {
    resetRoute();
    const path = $(`#${platforms[selected].path}`);
    if (!path) return;
    if (!motionAllowed() || !isOnScreen(network)) { finishRoute(); return; }
    const length = path.getTotalLength();
    let start, previous = -1;
    routeRunning = true;
    if (run) run.disabled = true;
    if (tracer) tracer.style.opacity = '1';
    if (halo) halo.style.opacity = '.18';
    if (trail) trail.style.opacity = '.65';
    function tick(now) {
      if (!motionAllowed() || !isOnScreen(network)) { finishRoute(); return; }
      start ??= now;
      const progress = Math.min(1, (now - start) / 2000);
      const eased = progress < .5 ? 2 * progress * progress : 1 - Math.pow(-2 * progress + 2, 2) / 2;
      const point = path.getPointAtLength(length * eased);
      for (const node of [tracer, halo]) if (node) {
        node.setAttribute('cx', point.x.toFixed(2));
        node.setAttribute('cy', point.y.toFixed(2));
      }
      trail?.setAttribute('stroke-dasharray', `${Math.max(.1, length * eased)} ${length + 1}`);
      const stage = Math.min(2, Math.floor(progress * 3));
      if (stage !== previous) {
        previous = stage;
        steps.forEach((node, index) => node.classList.toggle('zh23-is-active', index === stage));
        routeStatus(`${scenes[scenario].steps[stage]} · 逻辑模拟中`, 'running');
      }
      if (progress < 1) routeFrame = requestAnimationFrame(tick);
      else finishRoute();
    }
    routeFrame = requestAnimationFrame(tick);
  }
  run?.addEventListener('click', replayRoute);
  all('[data-scenario]').forEach(button => button.addEventListener('click', () => {
    scenario = button.dataset.scenario;
    all('[data-scenario]').forEach(node => {
      const on = node === button;
      node.classList.toggle('on', on);
      node.setAttribute('aria-pressed', String(on));
    });
    replayRoute();
  }));

  const ledgerStates = {
    settled: ['接收用量', '以返回的 usage 为依据。', '记录结算', '按已知消耗，完成结算。', 'SETTLED', '确认用量后结算，记录每一步。', 2],
    unknown: ['未返回用量', '调用已发生，用量待确认。', '挂起待核对', '保留 unknown，不擅自归零。', 'UNKNOWN', '用量缺失不等于免费。保留 unknown，等待核对。', 1],
    reject: ['请求未放行', '未通过授权或额度规则。', '不发往上游', '记录拒绝，不捏造消耗。', 'REJECTED', '未通过准入，不转发上游。', 0],
  };
  all('[data-ledger]').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.ledger, values = ledgerStates[key];
    if (!values) return;
    all('[data-ledger]').forEach(node => {
      node.classList.toggle('active', node === button);
      node.setAttribute('aria-pressed', String(node === button));
    });
    ['#ledgerMiddleTitle', '#ledgerMiddleDesc', '#ledgerLastTitle', '#ledgerLastDesc', '#ledgerBadge', '#ledgerDetail'].forEach((selector, index) => setText(selector, values[index]));
    $('#ledgerResult').className = 'ledger-result ' + (key === 'settled' ? '' : key);
    all('.ledger-card').forEach((node, index) => node.classList.toggle('focus', index === values[6]));
    $('.ledger-wrap').dataset.ledgerState = key;
    pulse($('.ledger-wrap'), 'is-state-changing', 450);
  }));

  const workspaceCopy = {
    overview: ['仪表盘', '账号健康、调用趋势与积分收支，一眼有数。'],
    accounts: ['账号', '按平台与分组管理账号，查看状态与可用资源。'],
    keys: ['密钥', '管理访问密钥、模型权限与使用额度。'],
    models: ['模型', '查看模型能力，按平台与条件筛选。'],
    usage: ['用量', '查看 Token 趋势与模型、密钥用量明细。'],
    logs: ['日志', '按请求记录追踪状态、用量与上游尝试。'],
    tasks: ['任务', '查看任务状态与执行记录。'],
    security: ['安全', '查看会话、访问规则与操作审计。'],
  };
  const workspaceButtons = all('[data-console]');
  function showWorkspace(button) {
    const key = button.dataset.console;
    if (!workspaceCopy[key]) return;
    workspaceButtons.forEach(node => {
      node.classList.toggle('active', node === button);
      node.setAttribute('aria-pressed', String(node === button));
    });
    all('.console-panel').forEach(node => {
      node.hidden = node.dataset.panel !== key;
      if (!node.hidden) pulse(node, 'is-entering', 350);
    });
    setText('#workspaceTitle', workspaceCopy[key][0]);
    setText('#workspaceDescription', workspaceCopy[key][1]);
  }
  workspaceButtons.forEach((button, index) => {
    button.addEventListener('click', () => showWorkspace(button));
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? workspaceButtons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + workspaceButtons.length) % workspaceButtons.length;
      workspaceButtons[next].focus({ preventScroll: true });
      workspaceButtons[next].scrollIntoView({ behavior: motionAllowed() ? 'smooth' : 'instant', block: 'nearest', inline: 'nearest' });
      showWorkspace(workspaceButtons[next]);
    });
  });
  all('[data-open-workspace]').forEach(button => button.addEventListener('click', () => {
    const tab = $(`[data-console="${button.dataset.openWorkspace}"]`);
    if (tab) showWorkspace(tab);
    $('#console')?.scrollIntoView({ behavior: motionAllowed() ? 'smooth' : 'instant', block: 'start' });
    tab?.focus({ preventScroll: true });
  }));

  all('.faq-question').forEach((button, index) => {
    const answer = button.closest('.faq-item').querySelector('.faq-answer');
    answer.id = `faq-answer-${index}`;
    button.setAttribute('aria-controls', answer.id);
    button.addEventListener('click', () => {
      const item = button.closest('.faq-item');
      const open = !item.classList.contains('open');
      all('.faq-item').forEach(node => {
        node.classList.remove('open');
        node.querySelector('.faq-question').setAttribute('aria-expanded', 'false');
      });
      item.classList.toggle('open', open);
      button.setAttribute('aria-expanded', String(open));
    });
  });

  const menu = $('#mobileMenu'), menuButton = $('#mobileTrigger');
  function closeMenu(restoreFocus = false) {
    menu?.classList.remove('open');
    menuButton?.setAttribute('aria-expanded', 'false');
    menuButton?.setAttribute('aria-label', '打开菜单');
    if (restoreFocus) menuButton?.focus({ preventScroll: true });
  }
  menuButton?.addEventListener('click', () => {
    const open = menu.classList.toggle('open');
    menuButton.setAttribute('aria-expanded', String(open));
    menuButton.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
  });
  menu?.querySelectorAll('a').forEach(link => link.addEventListener('click', () => closeMenu()));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && menu?.classList.contains('open')) closeMenu(true); });
  document.addEventListener('click', event => { if (menu?.classList.contains('open') && !event.target.closest('.navigation-wrap')) closeMenu(); });

  function syncMotion() {
    root.toggleAttribute('data-home-flow-paused', paused);
    root.toggleAttribute('data-reduced-motion', reduce.matches && !motionOverride);
    root.toggleAttribute('data-motion-override', motionOverride);
    root.toggleAttribute('data-page-hidden', document.hidden);
    if (motionButton) {
      motionButton.hidden = false;
      motionButton.disabled = false;
      motionButton.setAttribute('aria-pressed', String(paused || (reduce.matches && !motionOverride)));
      motionButton.textContent = reduce.matches && !motionOverride ? '开启动效' : paused ? '开启动效' : '暂停动效';
    }
    if (!motionAllowed() && routeRunning) finishRoute();
    if (!motionAllowed()) {
      for (const name of ['is-selecting', 'is-entering', 'is-state-changing', 'is-code-changing']) {
        all(`.${name}`).forEach(node => node.classList.remove(name));
      }
    }
    document.dispatchEvent(new CustomEvent('zhihui:motion-change'));
  }
  motionButton?.addEventListener('click', () => {
    if (reduce.matches && !motionOverride) { motionOverride = true; paused = false; }
    else paused = !paused;
    syncMotion();
  });
  document.addEventListener('zhihui:motion-request', event => {
    if (event.detail?.play !== true) return;
    motionOverride = true; paused = false; syncMotion();
  });
  reduce.addEventListener('change', () => { motionOverride = false; syncMotion(); });
  document.addEventListener('visibilitychange', syncMotion);
  if ('IntersectionObserver' in window) {
    root.classList.add('motion-ready');
    const revealObserver = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('visible'); revealObserver.unobserve(entry.target); }
    }), { threshold: .06 });
    for (const group of all('.feature-grid,.steps,.architecture-layout,.code-layout,.boundary')) {
      [...group.children].filter(node => node.classList.contains('reveal')).forEach((node, index) => node.style.setProperty('--reveal-delay', `${Math.min(index, 3) * 90}ms`));
    }
    all('.reveal').forEach(node => revealObserver.observe(node));
    const scopeObserver = new IntersectionObserver(entries => entries.forEach(entry => {
      entry.target.classList.toggle('motion-inview', entry.isIntersecting);
      if (entry.target === network && !entry.isIntersecting && routeRunning) finishRoute();
    }), { threshold: 0 });
    all('[data-motion-scope]').forEach(node => scopeObserver.observe(node));
    if (network) scopeObserver.observe(network);
  } else all('.reveal').forEach(node => node.classList.add('visible'));
  let scrollFrame = 0;
  function updateScroll() {
    scrollFrame = 0;
    const max = Math.max(1, root.scrollHeight - innerHeight);
    const progress = $('#scrollProgress');
    if (progress) progress.style.width = `${Math.min(1, scrollY / max) * 100}%`;
    $('.nav')?.classList.toggle('is-scrolled', scrollY > 30);
    let current;
    for (const link of all('.nav-center a')) {
      const target = $(link.getAttribute('href'));
      if (target?.getBoundingClientRect().top < 180) current = link;
    }
    all('.nav-center a').forEach(link => { if (link === current) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current'); });
  }
  addEventListener('scroll', () => { if (!scrollFrame) scrollFrame = requestAnimationFrame(updateScroll); }, { passive: true });
  addEventListener('resize', () => { if (innerWidth > 760) closeMenu(); updateScroll(); }, { passive: true });
  choosePlatform('workbuddy', 'init');
  syncMotion();
  updateScroll();
})();
