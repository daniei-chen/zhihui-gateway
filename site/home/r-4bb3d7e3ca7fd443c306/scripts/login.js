// Same-origin production contract preserved; local design previews never send credentials.
const form = document.getElementById('loginForm');
if (form) {
  const root = document.documentElement;
  const username = document.getElementById('username');
  const password = document.getElementById('password');
  const toggle = document.getElementById('togglePassword');
  const caps = document.getElementById('capsWarning');
  const status = document.getElementById('formStatus');
  const submit = document.getElementById('submitBtn');
  const label = document.getElementById('submitLabel');
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname) || location.protocol !== 'https:';
  const connected = root.dataset.authMode === 'same-origin' && !local;
  let busy = false, pageActive = true;
  const signal = state => document.dispatchEvent(new CustomEvent('zhihui:login-state', { detail: state }));
  function message(text = '', kind = 'info') {
    status.textContent = text; status.dataset.kind = kind; status.hidden = !text;
  }
  function setBusy(value) {
    busy = value; submit.disabled = value; submit.dataset.loading = String(value);
    form.setAttribute('aria-busy', String(value));
    label.textContent = value ? '登录中…' : '进入工作台';
    toggle.disabled = value; username.readOnly = value; password.readOnly = value;
  }
  function invalid(field, text) {
    field.setAttribute('aria-invalid', 'true'); message(text, 'error'); field.focus();
  }
  function hidePassword() {
    password.type = 'password'; toggle.setAttribute('aria-pressed', 'false'); toggle.setAttribute('aria-label', '显示密码');
    toggle.querySelector('span').textContent = '显示';
  }
  function readDetail(data, fallback) { return typeof data?.detail === 'string' && data.detail.trim() ? data.detail : fallback; }
  toggle.addEventListener('click', () => {
    const show = password.type === 'password';
    password.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(show)); toggle.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    toggle.querySelector('span').textContent = show ? '隐藏' : '显示'; password.focus({ preventScroll: true });
  });
  password.addEventListener('keyup', event => { caps.hidden = !event.getModifierState?.('CapsLock'); });
  password.addEventListener('blur', () => { caps.hidden = true; });
  [username, password].forEach(field => field.addEventListener('input', () => {
    field.removeAttribute('aria-invalid'); if (status.dataset.kind === 'error') message();
  }));
  form.addEventListener('focusin', () => signal('focused'));
  form.addEventListener('focusout', () => queueMicrotask(() => { if (!form.contains(document.activeElement) && !busy) signal('idle'); }));
  if (connected) document.getElementById('loginPreviewNote').hidden = true;
  if (new URLSearchParams(location.search).get('expired') === '1') message('登录状态已过期，请重新登录。');
  if (connected) fetch('/api/me', { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
    .then(response => { if (response.ok && !busy && pageActive) location.replace('/dashboard'); }).catch(() => {});
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const name = username.value.trim(), secret = password.value;
    username.removeAttribute('aria-invalid'); password.removeAttribute('aria-invalid');
    if (!name) { invalid(username, '请输入用户名。'); return; }
    if (!secret) { invalid(password, '请输入密码。'); return; }
    if (!connected) { message('这是本地设计预览，未提交账号或密码。实际登录请前往线上智汇控制台。'); return; }
    setBusy(true); message(); signal('submitting');
    let succeeded = false;
    try {
      const response = await fetch('/api/login', { method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ username: name, password: secret }) });
      const data = await response.json().catch(() => null);
      if (response.ok && data?.ok === true) {
        succeeded = true; hidePassword(); label.textContent = '进入中…';
        message('登录成功，正在进入工作台…', 'success'); signal('success');
        // Auth success alone triggers navigation; model readiness never gates login.
        if (pageActive) location.replace('/dashboard');
      } else if (response.status === 401) {
        password.value = ''; hidePassword(); invalid(password, readDetail(data, '用户名或密码错误，请重试。')); signal('error');
      } else {
        const fallback = response.status === 429 ? '登录尝试过于频繁，请稍后再试。' : response.status === 400 ? '请求格式错误，请刷新页面后重试。' : response.status >= 500 ? '服务暂时不可用，请稍后再试。' : '登录失败，请稍后重试。';
        message(response.status >= 500 ? fallback : readDetail(data, fallback), 'error'); signal('error');
      }
    } catch { message('无法连接服务器，请检查网络后重试。', 'error'); signal('error'); }
    finally { if (!succeeded) setBusy(false); }
  });
  addEventListener('pagehide', () => { pageActive = false; hidePassword(); });
  addEventListener('pageshow', () => { pageActive = true; });
  document.addEventListener('visibilitychange', () => { if (document.hidden) hidePassword(); });
  setBusy(false);
}
