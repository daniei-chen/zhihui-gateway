/* 极简 hash 路由：注册 → 渲染，支持页面参数与「离开时清理」。 */
const routes = new Map();
let current = null;
let cleanup = null;

export function register(path, page) {
  routes.set(path, page);
}

export function navigate(path) {
  if (location.hash === '#' + path) {
    render();
    return;
  }
  location.hash = path;
}

export function currentRoute() {
  return current;
}

function parse() {
  const raw = location.hash.replace(/^#\/?/, '') || 'overview';
  const [path, query = ''] = raw.split('?');
  const params = Object.fromEntries(new URLSearchParams(query));
  return { path: path || 'overview', params };
}

export async function render(ctx) {
  const { path, params } = ctx || parse();
  const page = routes.get(path) || routes.get('overview');
  if (!page) return;
  if (cleanup) {
    try { cleanup(); } catch { /* 忽略清理异常 */ }
    cleanup = null;
  }
  current = path;
  document.dispatchEvent(new CustomEvent('qtg:route', { detail: { path, params } }));
  const result = await page({ params, path });
  if (typeof result === 'function') cleanup = result;
  return cleanup;  // 交给调用方（测试/严格清理场景）显式释放
}

export function start() {
  window.addEventListener('hashchange', () => render());
  return render();
}
