const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

function hookHarness() {
  const slots = [];
  let cursor = 0;
  let queued = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const react = {
    useState(initial) {
      const index = cursor++;
      slots[index] ??= {value: typeof initial === 'function' ? initial() : initial};
      return [slots[index].value, next => { slots[index].value = typeof next === 'function' ? next(slots[index].value) : next; }];
    },
    useRef(initial) { const index = cursor++; slots[index] ??= {current: initial}; return slots[index]; },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = {value: callback, deps};
      return slots[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) {
        queued.push(() => { slots[index]?.cleanup?.(); slots[index] = {deps, effect, cleanup: effect()}; });
      }
    },
    createContext: () => ({Provider: 'provider'}),
  };
  return {
    react,
    render(fn) { cursor = 0; return fn(); },
    effects() { const pending = queued; queued = []; pending.forEach(fn => fn()); },
    replayEffects() {
      slots.forEach(slot => slot?.cleanup?.());
      slots.forEach(slot => { if (slot?.effect) slot.cleanup = slot.effect(); });
    },
    unmount() { slots.forEach(slot => slot?.cleanup?.()); },
  };
}

function load(relative, mocks) {
  const source = fs.readFileSync(path.join(root, relative), 'utf8');
  const code = ts.transpileModule(source, {fileName: relative, compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const compiled = {exports: {}};
  const context = {module: compiled, exports: compiled.exports, Promise, Error, console,
    require(name) { if (!(name in mocks)) throw new Error(`Unexpected dependency ${name}`); return mocks[name]; },
    ...mocks.globals,
  };
  vm.runInNewContext(code, context, {filename: relative});
  return compiled.exports;
}

test('usage cache rates preserve missing data, true zero and provider denominator', () => {
  const {formatCacheHitRate} = load('lib/usage-metrics.ts', {});
  assert.equal(formatCacheHitRate({cache_hit_tokens: 94, cache_miss_tokens: 6}), '94%');
  assert.equal(formatCacheHitRate({cache_hit_tokens: 0, cache_miss_tokens: 100}), '0%');
  assert.equal(formatCacheHitRate({cache_hit_tokens: 0, cache_miss_tokens: 0}), '—');
  assert.equal(formatCacheHitRate({cache_hit_tokens: 100}), '—');
  assert.equal(formatCacheHitRate({cache_hit_rate: null, cache_hit_tokens: 10, cache_miss_tokens: 0}), '—');
  for (const value of [NaN, Infinity, -1, 1.5, '1', true]) {
    assert.equal(formatCacheHitRate({cache_hit_rate: value}), '—');
  }
});

test('request cache incidence never falls back to token-weighted efficiency', () => {
  const {formatCacheRequestRate} = load('lib/usage-metrics.ts', {});
  assert.equal(formatCacheRequestRate({cache_hit_requests: 9, cache_observed_requests: 10}), '90.0%');
  assert.equal(formatCacheRequestRate({cache_hit_requests: 0, cache_observed_requests: 10}), '0.0%');
  assert.equal(formatCacheRequestRate({cache_hit_requests: 7033, cache_observed_requests: 7488}), '93.9%');
  for (const row of [{cache_hit_rate: .66}, {cache_hit_requests: 1},
    {cache_hit_requests: 1, cache_observed_requests: 0}, {cache_hit_requests: 11, cache_observed_requests: 10},
    {cache_hit_requests: -1, cache_observed_requests: 10}, {cache_hit_requests: .5, cache_observed_requests: 1},
    {cache_hit_requests: true, cache_observed_requests: 1}, {cache_hit_requests: 1, cache_observed_requests: Infinity}]) {
    assert.equal(formatCacheRequestRate(row), '—');
  }
});

test('provider total tokens never becomes a fabricated output count or unknown zero', () => {
  const {usageTokenTotal} = load('lib/usage-metrics.ts', {});
  assert.equal(usageTokenTotal({total_tokens: 190, prompt_tokens: null, completion_tokens: null}), 190);
  assert.equal(usageTokenTotal({total_tokens: 0, prompt_tokens: 20, completion_tokens: 30}), 0);
  assert.equal(usageTokenTotal({prompt_tokens: 20, completion_tokens: 30}), 50);
  for (const total_tokens of [null, -1, NaN, Infinity, true, '12']) {
    assert.equal(usageTokenTotal({total_tokens, prompt_tokens: 20, completion_tokens: 30}), null);
  }
  for (const row of [{}, {prompt_tokens: null, completion_tokens: 30}, {prompt_tokens: 20},
                     {prompt_tokens: Infinity, completion_tokens: 0}]) assert.equal(usageTokenTotal(row), null);
  const {dailySeries, hourlySeries} = load('lib/usage-chart.ts', {});
  const points = dailySeries([{day: '2026-10-30', total_tokens: 190, prompt_tokens: 30, completion_tokens: 50}], 1);
  assert.equal(points[0].tokens, 190);
  const hourly = hourlySeries([{day: '2026-10-30', hour: 0, total_tokens: 190, prompt_tokens: 30, completion_tokens: 50}]);
  assert.equal(hourly[0].tokens, 190);
  assert.equal(hourly[1].tokens, 0);
  assert.equal(hourlySeries([{hour: 0, total_tokens: 0, prompt_tokens: 30, completion_tokens: 50}])[0].tokens, 0);
});

test('Qoder hourly highlight follows the engine calendar, not the browser timezone', () => {
  const source = fs.readFileSync(path.join(root, 'app/(main)/stats/page.tsx'), 'utf8');
  assert.match(source, /hourlySeries\(hourly, platform === 'qoder' \? hourly\[0\]\?\.current_hour \?\? undefined : nowTick\?\.hour\)/);
});

test('user password reset keeps root policy distinct, preserves raw input and clears on close', async () => {
  const policy = load('lib/managed-user-password.ts', {});
  const jsx = (type, props) => ({type, props});
  for (const username of ['admin', 'alice']) {
    const h = hookHarness(), writes = [];
    const {ResetPasswordDialog} = load('components/common/settings/ResetPasswordDialog.tsx', {
      react: {...h.react, useId: () => 'password-hint'}, 'react/jsx-runtime': {jsx, jsxs: jsx},
      '@/components/common/layout/ConfirmDialog': {ConfirmDialog: 'dialog'},
      '@/components/ui/input': {Input: 'input'}, '@/lib/base-path': {BASE_PATH: ''},
      '@/lib/managed-user-password': policy, '@/lib/i18n/provider': {useT: () => key => key},
      '@/lib/toast': {notify: {ok() {}, err() {}}},
      '@/lib/api': {errText: String, settingsApi: {async updateUser(name, payload) {
        writes.push({name, payload}); return {relogin_required: false};
      }}},
    });
    const render = () => h.render(() => ResetPasswordDialog({username, trigger: null}));
    const input = tree => tree.props.content.props.children[0];
    let tree = render();
    assert.equal(tree.props.confirmDisabled, true);
    assert.equal(input(tree).props.maxLength, undefined);
    assert.equal(input(tree).props.inputMode, undefined);
    assert.equal(input(tree).props.pattern, undefined);
    assert.equal(tree.props.description, username === 'admin' ? 'settings.resetPasswordDesc' : 'settings.resetManagedPasswordDesc');
    for (const value of ['1234567', 'letters', '\u{1f600}'.repeat(3)]) {
      input(tree).props.onChange({target: {value}});
      tree = render();
      assert.equal(input(tree).props.value, value);
      assert.equal(tree.props.confirmDisabled, true);
      await tree.props.onConfirm();
    }
    assert.equal(writes.length, 0);
    if (username !== 'admin') {
      for (const value of ['123456789', '1234567890', '12345678901', 'long-mixed-password',
        '\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b', '\u{1f600}'.repeat(8)]) {
        input(tree).props.onChange({target: {value}});
        tree = render();
        assert.equal(input(tree).props.value, value);
        assert.equal(tree.props.confirmDisabled, false);
      }
      input(tree).props.onChange({target: {value: '\u{1f600}'.repeat(7)}});
      tree = render();
      assert.equal(tree.props.confirmDisabled, true);
    }
    input(tree).props.onChange({target: {value: username === 'admin' ? 'Root-Password!' : 'mixed123'}});
    tree = render();
    assert.equal(tree.props.confirmDisabled, false);
    await tree.props.onConfirm();
    assert.deepEqual(JSON.parse(JSON.stringify(writes[0])), {name: username, payload: {password: username === 'admin' ? 'Root-Password!' : 'mixed123'}});
    tree.props.onOpenChange(false);
    assert.equal(input(render()).props.value, '');
  }
});

test('five languages explain managed password minimum and translate the backend rejection', () => {
  const phrase = '用户登录密码至少 8 位';
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const dict = JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json'), 'utf8'));
    for (const key of ['managedPasswordHint', 'managedPasswordPlaceholder', 'resetManagedPasswordDesc']) {
      assert.match(dict.settings[key], /8/, locale + ':' + key);
    }
    if (locale === 'zh-CN') assert.deepEqual(dict.phrases, {});
    else assert.ok(dict.phrases[phrase], locale);
    assert.match(dict.settings.resetPasswordDesc, /12/, locale + ':root policy');
  }
});

test('output speed never displays absent or nonfinite measurements as zero', () => {
  const {formatOutputRate} = load('lib/usage-metrics.ts', {});
  assert.equal(formatOutputRate(37.5), '37.5');
  for (const value of [null, undefined, 0, -1, NaN, Infinity, '30', true]) {
    assert.equal(formatOutputRate(value), '—');
  }
});

test('usage page contains no operational or process-lifetime stats fetches', () => {
  const source = fs.readFileSync(path.join(root, 'app/(main)/stats/page.tsx'), 'utf8');
  assert.doesNotMatch(source, /OutcomeStatsPanel|UpstreamMetric|statsApi\.upstream\(/);
  assert.match(source, /formatCacheHitRate\(it\)/);
  assert.match(source, /formatCacheRequestRate\(it\)/);
  assert.match(source, /stats.cacheTokenShare/);
  assert.match(source, /stats.cacheScopeHint/);
  assert.match(source, /formatOutputRate\(it.tokens_per_sec\)/);
  assert.match(source, /currentKeysHint/);
  assert.match(source, /dailySeries\(daily, d, platform === 'qoder' \? undefined : nowTick\?\.today\)/);
});

function asyncHook() {
  const harness = hookHarness();
  const state = load('lib/async-state.ts', {});
  const {useAsyncAll} = load('lib/use-async-data.ts', {react: harness.react, '@/lib/async-state': state});
  return {harness, useAsyncAll};
}

test('pausing a completed scope neither refetches nor replaces its form source on resume', async () => {
  const {harness, useAsyncAll} = asyncHook();
  let enabled = false, calls = 0;
  const render = () => harness.render(() => useAsyncAll({cfg: async () => ({version: ++calls})}, [], [], {enabled}));
  render(); harness.effects(); await flush();
  assert.equal(calls, 0);
  enabled = true; render(); harness.effects(); await flush();
  const cfg = render().values.cfg;
  assert.equal(calls, 1);
  enabled = false; render(); harness.effects(); await flush();
  enabled = true; render(); harness.effects(); await flush();
  assert.equal(calls, 1);
  assert.equal(render().values.cfg, cfg);
});

test('pausing an in-flight scope rejects late writes and resumes a replacement', async () => {
  const {harness, useAsyncAll} = asyncHook();
  let enabled = true;
  const requests = [];
  const render = () => harness.render(() => useAsyncAll({cfg: () => {
    const work = deferred(); requests.push(work); return work.promise;
  }}, [], [], {enabled}));
  render(); harness.effects(); await flush();
  enabled = false; render(); harness.effects(); await flush();
  requests[0].resolve('old'); await flush();
  assert.equal(render().values.cfg, undefined);
  enabled = true; render(); harness.effects(); await flush();
  assert.equal(requests.length, 2);
  requests[1].resolve('new'); await flush();
  assert.equal(render().values.cfg, 'new');
});

test('slow initial success is not starved by repeated background refreshes', async () => {
  const {harness, useAsyncAll} = asyncHook();
  const work = deferred();
  let issued = 0;
  const fetchers = {count: () => { issued++; return work.promise; }};
  const render = () => harness.render(() => useAsyncAll(fetchers, ['cn']));
  let state = render(); harness.effects(); await flush();
  const first = state.refresh();
  assert.equal(state.refresh(), first);
  assert.equal(state.refresh(), first);
  assert.equal(issued, 1);
  work.resolve(42); await first; await flush();
  state = render();
  assert.equal(state.values.count, 42);
  assert.equal(state.isInitialLoading, false);
  assert.equal(state.isRefreshing, false);
});

test('fast cards render before slow billing finishes and stale partial writes cannot win', async () => {
  const {harness, useAsyncAll} = asyncHook();
  const oldFast = deferred(), oldSlow = deferred(), nextFast = deferred(), nextSlow = deferred();
  let realm = 'cn';
  const render = () => harness.render(() => useAsyncAll({
    pool: () => realm === 'cn' ? oldFast.promise : nextFast.promise,
    billing: () => realm === 'cn' ? oldSlow.promise : nextSlow.promise,
  }, [realm], [], {progressive: true}));
  render(); harness.effects(); await flush();
  oldFast.resolve(34); await flush();
  assert.equal(render().values.pool, 34);
  assert.equal(render().isInitialLoading, false);
  assert.equal(render().isRefreshing, true);
  realm = 'global'; render(); harness.effects(); await flush();
  assert.equal(render().values.pool, undefined);
  oldSlow.resolve(999); await flush();
  assert.equal(render().values.billing, undefined);
  nextFast.resolve(4); await flush();
  assert.equal(render().values.pool, 4);
  nextSlow.reject(new Error('billing unavailable')); await flush();
  assert.equal(render().values.pool, 4);
  assert.match(render().errors.billing.message, /billing unavailable/);
  assert.equal(render().isRefreshing, false);
});

test('only audited dashboard opts into incremental fields and pending pool is not displayed as zero', () => {
  const page = fs.readFileSync(path.join(root, 'app/(main)/dashboard/page.tsx'), 'utf8');
  assert.match(page, /progressive: true/);
  assert.match(page, /poolFailed \|\| poolPending/);
  assert.match(page, /summary \? fmtCompact\(summary.today_tokens\) : '—'/);
});

test('explicit post-mutation reload supersedes old work; stale result cannot win', async () => {
  const {harness, useAsyncAll} = asyncHook();
  const requests = [];
  const fetchers = {count: () => { const d = deferred(); requests.push(d); return d.promise; }};
  const render = () => harness.render(() => useAsyncAll(fetchers, ['cn']));
  const state = render(); harness.effects(); await flush();
  const latest = state.reload(); await flush();
  assert.equal(requests.length, 2);
  requests[1].resolve(8); await latest;
  requests[0].resolve(1); await flush();
  assert.equal(render().values.count, 8);
});

test('changed data context clears old values and rejects late previous-context results', async () => {
  const {harness, useAsyncAll} = asyncHook();
  const cn = deferred(), global = deferred();
  let realm = 'cn';
  const render = () => harness.render(() => useAsyncAll({count: () => realm === 'cn' ? cn.promise : global.promise}, [realm]));
  render(); harness.effects(); await flush();
  realm = 'global'; render(); harness.effects(); await flush();
  cn.resolve(999); await flush();
  assert.equal(render().values.count, undefined);
  global.resolve(7); await flush();
  assert.equal(render().values.count, 7);
});

test('synchronous fetch failure becomes a visible error and unmounted work is ignored', async () => {
  const {harness, useAsyncAll} = asyncHook();
  let state = harness.render(() => useAsyncAll({count: () => { throw new Error('sync failure'); }}, []));
  harness.effects(); await flush();
  state = harness.render(() => useAsyncAll({}, []));
  assert.equal(state.isInitialFailed, true);
  assert.match(state.errors.count.message, /sync failure/);
  const next = deferred();
  state = harness.render(() => useAsyncAll({count: () => next.promise}, []));
  const pending = state.reload(); await flush(); harness.unmount();
  next.resolve(88); await pending;
  assert.equal(harness.render(() => useAsyncAll({}, [])).values.count, undefined);
});

test('heartbeat is single-flight, resumes after rejection, and skips hidden tabs', async () => {
  const harness = hookHarness();
  let tick, visibility, calls = 0;
  const work = deferred();
  const document = {hidden: false, addEventListener: (_name, fn) => { visibility = fn; }, removeEventListener() {}};
  const window = {setInterval: fn => { tick = fn; return 1; }, clearInterval() {}};
  const {useHeartbeat} = load('lib/use-heartbeat.ts', {react: harness.react, globals: {document, window}});
  harness.render(() => useHeartbeat(() => { calls++; return work.promise; }, 30000)); harness.effects();
  tick(); visibility(); tick(); await flush();
  assert.equal(calls, 1);
  work.reject(new Error('offline')); await flush();
  document.hidden = true; tick(); await flush(); assert.equal(calls, 1);
  document.hidden = false; visibility(); await flush(); assert.equal(calls, 2);
});

test('Strict Mode effect replay starts a replacement load and cannot settle stale data', async () => {
  const {harness, useAsyncAll} = asyncHook();
  const requests = [];
  const render = () => harness.render(() => useAsyncAll({count: () => {
    const work = deferred(); requests.push(work); return work.promise;
  }}, []));
  render(); harness.effects(); await flush();
  harness.replayEffects(); await flush();
  assert.equal(requests.length, 2);
  requests[0].resolve(99); await flush(); assert.equal(render().values.count, undefined);
  requests[1].resolve(7); await flush(); assert.equal(render().values.count, 7);
});

test('partial refresh preserves a previously successful field and exposes the failed field', async () => {
  const {harness, useAsyncAll} = asyncHook();
  let fail = false;
  const render = () => harness.render(() => useAsyncAll({
    first: async () => fail ? 11 : 10,
    second: async () => { if (fail) throw new Error('offline'); return 20; },
  }, []));
  render(); harness.effects(); await flush();
  fail = true; assert.equal(await render().refresh(), false);
  const state = render();
  assert.equal(state.values.first, 11); assert.equal(state.values.second, 20);
  assert.match(state.errors.second.message, /offline/);
  assert.equal(state.isInitialFailed, false);
});

test('queued heartbeat does not call an unmounted page', async () => {
  const harness = hookHarness();
  let tick, calls = 0;
  const document = {hidden: false, addEventListener() {}, removeEventListener() {}};
  const window = {setInterval: fn => { tick = fn; return 1; }, clearInterval() {}};
  const {useHeartbeat} = load('lib/use-heartbeat.ts', {react: harness.react, globals: {document, window}});
  harness.render(() => useHeartbeat(() => { calls++; }, 10)); harness.effects();
  tick(); harness.unmount(); await flush(); assert.equal(calls, 0);
});

test('API platform query defaults preserve explicit values and storage failure does not break 401', async () => {
  let requestInterceptor, responseError, explicit = 'qoder';
  const axios = {create: () => ({interceptors: {
    request: {use: fn => { requestInterceptor = fn; }},
    response: {use: (_success, failure) => { responseError = failure; }},
  }})};
  const window = {sessionStorage: {getItem() { throw new Error('denied'); }}, location: {pathname: '/dashboard', href: '/dashboard'}};
  load('lib/api.ts', {axios: {default: axios}, './base-path': {BASE_PATH: ''}, './err-envelope': {}, './i18n': {},
    './public-demo': {}, './platform-store': {platformStore: {explicit: () => explicit}},
    globals: {window, URLSearchParams, process: {env: {}}}});
  for (const params of [undefined, {}, {platform: undefined}]) {
    const config = requestInterceptor({headers: {}, params});
    assert.equal(config.headers['X-QTG-Platform'], 'qoder'); assert.equal(config.params.platform, 'qoder');
  }
  const override = requestInterceptor({headers: {}, params: {platform: 'trae', page: 2}});
  assert.equal(override.params.platform, 'trae'); assert.equal(override.params.page, 2);
  const captured = requestInterceptor({headers: {'X-QTG-Platform': 'trae'}, params: {platform: 'trae', realm: 'cn'}});
  assert.equal(captured.headers['X-QTG-Platform'], 'trae', 'A captured authorization must not move to the current qoder store');
  assert.equal(captured.params.platform, 'trae');
  const encoded = requestInterceptor({headers: {}, params: new URLSearchParams('realm=global')});
  assert.equal(encoded.params.get('platform'), 'qoder'); assert.equal(encoded.params.get('realm'), 'global');
  explicit = null;
  assert.equal(requestInterceptor({headers: {}}).params, undefined);
  await assert.rejects(responseError({response: {status: 401}}));
  assert.equal(window.location.href, '/login');
});

test('authorization popup nulls its opener and never reads a cross-origin location', () => {
  const navigated = [];
  const child = {opener: 'fixture', location: {replace: value => navigated.push(value)}, focus() {}, close() {}};
  const window = {screen: {availWidth: 1440, availHeight: 900}, screenX: 0, screenY: 0,
    outerWidth: 1440, outerHeight: 900, open: () => child};
  const {openAuthorizationWindow} = load('lib/authorization-window.ts', {globals: {window}});
  assert.equal(openAuthorizationWindow('https://qoder.com/device/selectAccounts'), true);
  assert.equal(child.opener, null);
  assert.deepEqual(navigated, ['https://qoder.com/device/selectAccounts']);
  window.open = () => null;
  assert.equal(openAuthorizationWindow('https://qoder.com/device/selectAccounts'), false, 'Blocked popups leave the ordinary link fallback');
});

test('logout failure keeps identity/cache/location; confirmed success clears them', async () => {
  const harness = hookHarness();
  const cache = new Map();
  const window = {sessionStorage: {
    getItem: key => cache.get(key) ?? null,
    setItem: (key, value) => cache.set(key, value),
    removeItem: key => cache.delete(key),
  }, location: {href: '/dashboard'}};
  let logoutImpl = async () => { throw new Error('network offline'); };
  const authApi = {me: async () => ({username: 'fixture', role: 'admin'}), logout: () => logoutImpl()};
  const {AuthProvider} = load('lib/auth-context.tsx', {
    react: harness.react, 'react/jsx-runtime': {jsx: (type, props) => ({type, props})},
    '@/lib/use-hydrated': {useHydrated: () => true},
    '@/lib/api': {authApi}, '@/lib/base-path': {BASE_PATH: ''}, globals: {window},
  });
  const render = () => harness.render(() => AuthProvider({children: null})).props.value;
  render(); harness.effects(); await flush();
  let state = render(); assert.equal(state.me.username, 'fixture');
  await assert.rejects(state.logout(), /network offline/);
  assert.equal(render().me.username, 'fixture'); assert.equal(window.location.href, '/dashboard'); assert.ok(cache.has('wb-me'));
  logoutImpl = async () => ({ok: false});
  await assert.rejects(state.logout(), /not confirmed/);
  assert.equal(render().me.username, 'fixture');
  logoutImpl = async () => ({ok: true}); await state.logout();
  assert.equal(render().me, null); assert.equal(cache.has('wb-me'), false); assert.equal(window.location.href, '/login');
});

test('all periodic page hooks use coalescing refresh, not explicit reload', () => {
  const directory = path.join(root, 'app/(main)');
  for (const name of fs.readdirSync(directory)) {
    const file = path.join(directory, name, 'page.tsx');
    if (!fs.existsSync(file)) continue;
    const source = fs.readFileSync(file, 'utf8');
    assert.doesNotMatch(source, /useHeartbeat\((?:reload|retry|upstream\.reload)/, name);
  }
});

test('dashboard reads group snapshots without billing POSTs and labels freshness', () => {
  const source = fs.readFileSync(path.join(root, 'app/(main)/dashboard/page.tsx'), 'utf8');
  assert.match(source, /accountApi\.cachedCredits\(g\.id \?\? null\)/);
  assert.doesNotMatch(source, /accountApi\.refreshCredits\(/);
  assert.doesNotMatch(source, /isAdmin\s*\?\s*loadGroupCredits/);
  assert.match(source, /t\('dashboard\.balanceSnapshotNote'\)/);
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const text = JSON.parse(fs.readFileSync(path.join(root, `lib/i18n/locales/${locale}.json`), 'utf8'));
    assert.ok(text.dashboard.balanceSnapshotNote, locale);
  }
});

test('backend error details remain authoritative and server diagnostics get localized context', () => {
  const {errText} = load('lib/api.ts', {
    axios: {default: {create: () => ({interceptors: {
      request: {use() {}}, response: {use() {}},
    }})}},
    './base-path': {BASE_PATH: ''},
    './err-envelope': {backendErrorText: body => typeof body?.detail === 'string' ? body.detail : '', channelCheckDetails: () => null},
    './i18n': {t: (key, params) => params ? `${key}: ${params.detail}` : key, tp: text => text},
    './platform-store': {},
    './public-demo': {}, globals: {process: {env: {}}},
  });
  assert.equal(errText({response: {status: 403, data: {detail: 'Only local calls are allowed'}}}), 'Only local calls are allowed');
  assert.equal(errText({response: {status: 403, data: {}}}), 'error.forbidden');
  assert.equal(errText({response: {status: 502, data: {detail: 'Fixture upstream is unavailable'}}}), 'error.serverDetail: Fixture upstream is unavailable');
});

test('selectively hydrated consumers retain their server snapshot before becoming live', () => {
  let hydration = true;
  const {useHydrated} = load('lib/use-hydrated.ts', {react: {useSyncExternalStore(subscribe, client, server) {
    assert.equal(typeof subscribe(), 'function'); return hydration ? server() : client();
  }}});
  assert.equal(useHydrated(), false);
  hydration = false;
  assert.equal(useHydrated(), true);
  const live = {me: {username: 'fixture', role: 'admin'}, loading: false, isAdmin: true, role: 'admin', logout() {}};
  let hydrated = false;
  const {useAuth} = load('lib/auth-context.tsx', {react: {...hookHarness().react, useContext: () => live},
    'react/jsx-runtime': {}, '@/lib/use-hydrated': {useHydrated: () => hydrated}, '@/lib/api': {}, '@/lib/base-path': {}});
  const initial = useAuth();
  assert.equal(initial.isAdmin, false); assert.equal(initial.me, null); assert.equal(initial.loading, true);
  assert.equal(initial.logout, live.logout);
  hydrated = true; assert.equal(useAuth(), live);
});

function evidenceHelpers() {
  return load('lib/operational-evidence.ts', {'./configuration-fields.json': {default: JSON.parse(fs.readFileSync(path.join(root, 'lib/configuration-fields.json'), 'utf8'))}, globals: {URL}});
}

test('evidence preserves unknowns, zero, stale and authorization boundaries', () => {
  const {evidenceValue, evidenceAccess} = evidenceHelpers();
  assert.equal(evidenceValue(null), 'Unknown'); assert.equal(evidenceValue(undefined), 'Unknown');
  assert.equal(evidenceValue(0), '0'); assert.equal(evidenceValue(false), 'false');
  for (const status of [401, 403]) assert.equal(evidenceAccess({response: {status}}), 'forbidden');
  for (const status of [404, 501]) assert.equal(evidenceAccess({response: {status}}), 'unavailable');
  for (const status of [500, 502, 504]) assert.equal(evidenceAccess({response: {status}}), 'stale');
  assert.equal(evidenceAccess(new Error('offline')), 'stale');
  assert.equal(evidenceAccess(undefined), null);
});

test('configuration display redacts secrets, unknown names, objects and credential-bearing URL parts', () => {
  const {safeConfigurationValue: safe, safeNamedConfigurationValue: named} = evidenceHelpers();
  assert.equal(safe('fixture-secret', true), '[redacted]');
  assert.equal(safe('fixture-secret', false, false), '[redacted]');
  assert.equal(safe({value: 'fixture-secret'}), '[redacted]');
  assert.equal(safe('https://user:secret@example.invalid/private?token=secret#secret'), 'https://example.invalid');
  assert.equal(named('UNKNOWN_FUTURE_SETTING', 'fixture-secret'), '[redacted]');
  const schema = JSON.parse(fs.readFileSync(path.join(root, 'lib/configuration-fields.json'), 'utf8'));
  for (const [name, spec] of Object.entries(schema)) if (spec.sensitive) assert.equal(named(name, 'fixture-secret'), '[redacted]', name);
  assert.equal(named('WB_HTTP_POOL_CONNECTIONS', 0), '0');
});

test('operational API stays on Manager routing and uses actual-platform ledger filter', async () => {
  const calls = [];
  let options;
  const {operationalApi} = load('lib/operational-api.ts', {axios: {default: {create(config) {
    options = config; return {async get(url, config) {calls.push({url, params: config.params}); return {data: {ok: true}};}};
  }}}, './base-path': {BASE_PATH: ''}});
  await operationalApi.outcomes('trae', 2, 'upstream.timeout', 7);
  await operationalApi.explain('execution-id-not-client-id');
  await operationalApi.stats(30, 'global');
  await operationalApi.configuration(); await operationalApi.drift();
  assert.equal(options.headers['X-QTG-Platform'], 'workbuddy');
  assert.equal(calls[0].params.upstream_platform, 'trae'); assert.equal(calls[0].params.platform, undefined);
  assert.equal(calls[0].params.page, 2); assert.equal(calls[0].params.error_code, 'upstream.timeout');
  assert.equal(calls[1].params.request_id, 'execution-id-not-client-id');
  assert.equal(calls[2].params.realm, 'global');
  assert.equal(calls[3].url, '/api/system/configuration'); assert.equal(calls[4].url, '/api/system/config');
});

test('evidence frame hides retained data on 403/501 but labels a failed refresh as stale', () => {
  const jsx = (type, props) => ({type, props});
  const {EvidenceFrame} = load('components/operations/EvidenceFrame.tsx', {
    'react/jsx-runtime': {jsx, jsxs: jsx}, '@/lib/i18n/provider': {useT: () => key => key},
    '@/lib/operational-evidence': evidenceHelpers(), '@/components/ui/button': {Button: 'button'},
  });
  const render = status => JSON.stringify(EvidenceFrame({title: 'Evidence', source: 'Fixture', error: {response: {status}}, reload() {}, children: 'RETAINED_FIXTURE_DATA'}));
  assert.doesNotMatch(render(403), /RETAINED_FIXTURE_DATA/); assert.match(render(403), /operations.forbidden/);
  assert.doesNotMatch(render(501), /RETAINED_FIXTURE_DATA/); assert.match(render(501), /operations.unavailable/);
  assert.match(render(500), /RETAINED_FIXTURE_DATA/); assert.match(render(500), /operations.stale/);
});

function descendants(node) {
  if (node === null || node === undefined || typeof node !== 'object') return [];
  if (Array.isArray(node)) return node.flatMap(descendants);
  return [node, ...descendants(node.props?.children)];
}

function createMemberFixture(fetch, saved = async () => {}) {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  class PortalError extends Error { constructor(status, message) {super(message); this.status = status;} }
  const components = Object.fromEntries(['Dialog', 'DialogBody', 'DialogContent', 'DialogDescription', 'DialogHeader', 'DialogTitle'].map(name => [name, name]));
  const icons = Object.fromEntries(['Check', 'Eye', 'EyeOff', 'ShieldCheck', 'Sparkles', 'UserPlus'].map(name => [name, name]));
  const {default: Create} = load('app/(main)/members/CreateMemberDialog.tsx', {
    react: {...h.react, useId: () => 'create-test'}, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': icons, '@/components/animate-ui/radix/dialog': components,
    '@/lib/portal-api': {PortalError, portalFetch: fetch},
    '@/lib/portal-members': load('lib/portal-members.ts', {}),
    globals: {crypto: {getRandomValues(bytes) {bytes.fill(7); return bytes;}}},
  });
  let closed = 0;
  const render = () => h.render(() => Create({close() {closed++;}, saved}));
  const nodes = () => descendants(render());
  const input = suffix => nodes().find(node => node.type === 'input' && node.props.id.endsWith(suffix));
  const form = () => nodes().find(node => node.type === 'form');
  return {render, nodes, input, form, PortalError, closed: () => closed};
}

test('create user keeps raw password and guards immediate duplicate submissions', async () => {
  const pending = deferred(), calls = [];
  let refreshed = 0;
  const f = createMemberFixture((path, options) => {calls.push({path, options}); return pending.promise;}, async () => {refreshed++;});
  f.input('username').props.onChange({target: {value: 'local-user'}});
  f.input('password').props.onChange({target: {value: '  raw  8'}});
  const submit = f.form().props.onSubmit;
  submit({preventDefault() {}}); submit({preventDefault() {}});
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].options.body), {username: 'local-user', password: '  raw  8'});
  assert.equal(f.input('password').props.disabled, true);
  pending.resolve({id: 'synthetic', username: 'local-user'});
  await flush();
  assert.equal(refreshed, 1);
  assert.match(JSON.stringify(f.render()), /用户已开通/);
  assert.doesNotMatch(JSON.stringify(f.render()), /  raw  8/);
  assert.equal(f.nodes().filter(node => node.type === 'input').length, 0);
});

test('eight-character weak passwords show exact server reason instead of fake length error', async () => {
  let f;
  f = createMemberFixture(async () => {throw new f.PortalError(400, '密码过于常见，请换一个');});
  f.input('username').props.onChange({target: {value: 'local-user'}});
  f.input('password').props.onChange({target: {value: '12345678'}});
  f.form().props.onSubmit({preventDefault() {}});
  await flush();
  assert.match(JSON.stringify(f.render()), /密码过于常见，请换一个/);
  assert.equal(f.input('password').props['aria-invalid'], true);
  assert.equal(f.input('password').props.value, '12345678');
  f.input('password').props.onChange({target: {value: '07392641'}});
  assert.equal(f.input('password').props['aria-invalid'], false);
  assert.doesNotMatch(JSON.stringify(f.render()), /密码过于常见，请换一个/);
});

test('creation remains successful when following user-list refresh fails', async () => {
  let calls = 0;
  const f = createMemberFixture(async () => {calls++; return {id: 'new', username: 'local-user'};}, async () => {throw new Error('refresh failed');});
  f.input('username').props.onChange({target: {value: 'local-user'}});
  f.input('password').props.onChange({target: {value: 'D7x!p9Q2'}});
  f.form().props.onSubmit({preventDefault() {}});
  await flush();
  assert.equal(calls, 1);
  assert.match(JSON.stringify(f.render()), /用户创建成功，但列表刷新失败/);
  assert.doesNotMatch(JSON.stringify(f.render()), /确认开通用户/);
});

test('creation cannot dismiss while the request is pending', async () => {
  const pending = deferred();
  const f = createMemberFixture(() => pending.promise);
  f.input('username').props.onChange({target: {value: 'local-user'}});
  f.input('password').props.onChange({target: {value: 'D7x!p9Q2'}});
  f.form().props.onSubmit({preventDefault() {}});
  let prevented = 0;
  const content = f.nodes().find(node => node.type === 'DialogContent');
  content.props.onEscapeKeyDown({preventDefault() {prevented++;}});
  content.props.onInteractOutside({preventDefault() {prevented++;}});
  f.render().props.onOpenChange(false);
  assert.equal(prevented, 2); assert.equal(f.closed(), 0);
  pending.resolve({id: 'new', username: 'local-user'}); await flush();
  f.render().props.onOpenChange(false); assert.equal(f.closed(), 1);
});

test('creation confirmation waits for its refresh before allowing completion', async () => {
  const refresh = deferred();
  const f = createMemberFixture(async () => ({id: 'new', username: 'local-user'}), () => refresh.promise);
  f.input('username').props.onChange({target: {value: 'local-user'}});
  f.input('password').props.onChange({target: {value: 'D7x!p9Q2'}});
  f.form().props.onSubmit({preventDefault() {}}); await flush();
  const finish = () => f.nodes().find(node => node.type === 'button' && node.props.children === '完成');
  assert.equal(finish().props.disabled, true);
  refresh.resolve(); await flush(); assert.equal(finish().props.disabled, false);
});

test('animated dialog keeps Radix lifecycle handlers off the motion DOM element', () => {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  const {DialogContent} = load('components/animate-ui/radix/dialog.tsx', {
    react: {...h.react, useContext: () => ({isOpen: true})},
    'react/jsx-runtime': {jsx, jsxs: jsx}, 'radix-ui': {Dialog: {Content: 'primitive-content'}},
    'lucide-react': {X: 'icon'}, 'motion/react': {AnimatePresence: 'presence', motion: {div: 'motion-div'}},
    '@/lib/utils': {cn: (...values) => values.filter(Boolean).join(' ')}, '@/components/ui/scroll-area': {ScrollArea: 'scroll'},
  });
  const handlers = Object.fromEntries(['onOpenAutoFocus', 'onCloseAutoFocus', 'onEscapeKeyDown',
    'onPointerDownOutside', 'onFocusOutside', 'onInteractOutside'].map(key => [key, () => {}]));
  const tree = h.render(() => DialogContent({...handlers, className: 'test-dialog', 'aria-label': 'test', children: 'content'}));
  const nodes = descendants(tree);
  const primitive = nodes.find(node => node.type === 'primitive-content');
  const motion = descendants(primitive).find(node => node.type === 'motion-div');
  for (const [key, handler] of Object.entries(handlers)) {
    assert.equal(primitive.props[key], handler, key);
    assert.equal(motion.props[key], undefined, key);
  }
  assert.equal(motion.props['aria-label'], 'test');
  assert.match(motion.props.className, /test-dialog/);
});

test('controlled dialogs stay open when the owner rejects dismissal', () => {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  let requested;
  const {Dialog} = load('components/animate-ui/radix/dialog.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'radix-ui': {Dialog: {Root: 'primitive-root'}},
    'lucide-react': {}, 'motion/react': {}, '@/lib/utils': {}, '@/components/ui/scroll-area': {},
  });
  const render = () => h.render(() => Dialog({open: true, onOpenChange: value => {requested = value;}, children: null}));
  render(); h.effects();
  render().props.children.props.onOpenChange(false);
  assert.equal(requested, false);
  assert.equal(render().props.value.isOpen, true);
});

test('uncontrolled dialogs still track their own open and close requests', () => {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  const {Dialog} = load('components/animate-ui/radix/dialog.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'radix-ui': {Dialog: {Root: 'primitive-root'}},
    'lucide-react': {}, 'motion/react': {}, '@/lib/utils': {}, '@/components/ui/scroll-area': {},
  });
  const render = () => h.render(() => Dialog({defaultOpen: false, children: null}));
  assert.equal(render().props.value.isOpen, false);
  render().props.children.props.onOpenChange(true); assert.equal(render().props.value.isOpen, true);
  render().props.children.props.onOpenChange(false); assert.equal(render().props.value.isOpen, false);
});

function passwordMemberFixture(fetch, saved = async () => {}) {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  const components = Object.fromEntries(['Dialog', 'DialogBody', 'DialogContent', 'DialogDescription', 'DialogHeader', 'DialogTitle'].map(name => [name, name]));
  const {default: Password} = load('app/(main)/members/PasswordDialog.tsx', {
    react: {...h.react, useId: () => 'reset-test'}, 'react/jsx-runtime': {jsx, jsxs: jsx},
    '@/components/animate-ui/radix/dialog': components,
    '@/lib/portal-api': {portalFetch: fetch}, '@/lib/portal-members': load('lib/portal-members.ts', {}),
  });
  let closed = 0;
  const render = () => h.render(() => Password({user: {id: 'local-user', username: 'local-user'}, close() {closed++;}, saved}));
  const nodes = () => descendants(render());
  const inputs = () => nodes().filter(node => node.type === 'input');
  const prepare = password => {
    inputs()[0].props.onChange({target: {value: password}});
    inputs()[1].props.onChange({target: {value: password}});
    inputs()[2].props.onChange({target: {checked: true}});
  };
  const form = () => nodes().find(node => node.type === 'form');
  return {render, nodes, inputs, prepare, form, closed: () => closed};
}

test('password reset stays successful and clears secrets when the list refresh fails', async () => {
  const calls = [];
  const f = passwordMemberFixture(async (path, options) => {calls.push({path, options});}, async () => {throw new Error('offline');});
  f.prepare('  raw  8'); f.form().props.onSubmit({preventDefault() {}}); await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].options.body), {password: '  raw  8'});
  assert.match(JSON.stringify(f.render()), /密码已重置，但列表刷新失败/);
  assert.doesNotMatch(JSON.stringify(f.render()), /  raw  8|确认重置密码/);
  assert.equal(f.inputs().length, 0);
  f.form().props.onSubmit({preventDefault() {}}); await flush(); assert.equal(calls.length, 1);
});

test('password reset shows authoritative rejection and cannot dismiss or submit twice while pending', async () => {
  const pending = deferred(); let calls = 0;
  const f = passwordMemberFixture(() => {calls++; return pending.promise;});
  f.prepare('12345678');
  const submit = f.form().props.onSubmit;
  submit({preventDefault() {}}); submit({preventDefault() {}});
  assert.equal(calls, 1);
  let prevented = 0;
  const content = f.nodes().find(node => node.type === 'DialogContent');
  content.props.onEscapeKeyDown({preventDefault() {prevented++;}});
  content.props.onInteractOutside({preventDefault() {prevented++;}});
  f.render().props.onOpenChange(false);
  assert.equal(prevented, 2); assert.equal(f.closed(), 0);
  pending.reject(new Error('密码过于常见，请换一个')); await flush();
  assert.match(JSON.stringify(f.render()), /密码过于常见，请换一个/);
  assert.equal(f.inputs()[0].props['aria-invalid'], true);
  assert.equal(f.inputs()[0].props.value, '12345678');
  f.inputs()[0].props.onChange({target: {value: 'D7x!p9Q2'}});
  assert.doesNotMatch(JSON.stringify(f.render()), /密码过于常见，请换一个/);
});

function portalLoginFixture(login) {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  const {default: Login} = load('app/portal/PortalLogin.tsx', {
    react: {...h.react, useId: () => 'login-test'}, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': Object.fromEntries(['ArrowUpRight','CalendarCheck','Eye','EyeOff','ScanLine','ShieldCheck'].map(name => [name,name])),
    '@/components/ui/button': {Button: 'button'}, '@/components/ui/input': {Input: 'input'}, '@/components/ui/label': {Label: 'label'},
  });
  const render = () => h.render(() => Login({login}));
  const form = () => descendants(render()).find(node => node.type === 'form');
  return {render, form};
}

test('portal login uses actual autofilled form values, preserves password and submits once', async () => {
  const pending=deferred(), calls=[]; let cleared=0;
  const f=portalLoginFixture((...args) => {calls.push(args); return pending.promise;});
  const form={elements:{namedItem: name => ({value:name === 'username' ? ' local-demo ' : '  raw  8'})},reset(){cleared++;}};
  const event={preventDefault(){},currentTarget:form};
  const submit=f.form().props.onSubmit;
  submit(event); submit(event); assert.equal(calls.length,1);
  assert.deepEqual(calls[0],['local-demo','  raw  8']);
  pending.resolve(); await flush(); assert.equal(cleared,1);
});

test('portal login exposes errors, does not erase rejected input, and rejects empty submissions locally', async () => {
  const f=portalLoginFixture(async () => {throw new Error('用户名或密码错误');});
  let cleared=0;
  const form={elements:{namedItem:()=>({value:''})},reset(){cleared++;}};
  const event={preventDefault(){},currentTarget:form};
  f.form().props.onSubmit(event); await flush(); assert.match(JSON.stringify(f.render()),/请填写用户名和密码/);
  form.elements.namedItem=()=>({value:'synthetic'});
  f.form().props.onSubmit(event); await flush(); assert.match(JSON.stringify(f.render()),/用户名或密码错误/);
  assert.equal(cleared,0);
  for(const input of descendants(f.render()).filter(node=>node.type==='input')) assert.equal(input.props['aria-invalid'],true);
});

test('portal reuses console brand and stat components with accessible tabs and truthful dispatch state', () => {
  const page=fs.readFileSync(path.join(root,'app/portal/page.tsx'),'utf8');
  assert.match(page,/<BrandHome href="\/portal"/);
  assert.match(page,/<StatCard/); assert.match(page,/<TabsList aria-label="用户工作台页面"/);
  assert.match(page,/data.dispatch_enabled/); assert.match(page,/data.pool_enabled/);
  assert.doesNotMatch(page,/本地候选|portal-mark|Your accounts\. Your credits\./);
  const login=fs.readFileSync(path.join(root,'app/portal/PortalLogin.tsx'),'utf8');
  assert.doesNotMatch(login,/localStorage|sessionStorage|console\.|maxLength/);
});

function portalPageFixture(fetch) {
  const h = hookHarness();
  const timers = new Map(); let timerId = 0;
  const jsx = (type, props) => ({type, props});
  class PortalError extends Error { constructor(status, message) { super(message); this.status = status; } }
  const api = {portalFetch: fetch, PortalError, points: String, portalTime: String, canCheckin: () => false, checkinLabel: String};
  const globals = {AbortController, URL, document: {hidden: false, activeElement: null, getElementById: () => null, addEventListener() {}, removeEventListener() {}},
    window: {setInterval(fn) {timers.set(++timerId, fn); return timerId;}, clearInterval(id) {timers.delete(id);}}};
  const workspace = load('lib/use-portal-workspace.ts', {react: h.react,
    './portal-api': api, './use-heartbeat': {useHeartbeat() {}}, globals});
  const {default: Portal} = load('app/portal/page.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'qrcode.react': {QRCodeSVG: 'qr'},
    'lucide-react': {}, '@/lib/portal-api': api, './portal.css': {},
    './PortalKeys': {default: 'keys'}, './PortalLogin': {default: 'login'}, './PortalRequests': {default: 'requests'},
    '@/lib/use-portal-workspace': workspace, '@/lib/portal-display': {checkinSummary: () => ({total: 0,ready: 0,pending: 0,confirmed: 0})},
    '@/components/common/layout/BrandHome': {BrandHome: 'brand'}, '@/components/common/layout/StatCard': {StatCard: 'stat'},
    '@/components/ui/tabs': Object.fromEntries(['Tabs','TabsContent','TabsList','TabsTrigger'].map(name => [name,name])),
    globals,
  });
  const render = () => h.render(() => {const outer = Portal(); return outer.type(outer.props);});
  return {h, render, PortalError, timers};
}

test('portal overview does not fetch inactive request history or depend on its availability', async () => {
  const calls = [];
  const f = portalPageFixture(async path => {
    calls.push(path);
    if (path === 'portal/me') return {id: 'fixture', username: 'fixture'};
    if (path === 'portal/overview') return {accounts: [], grants: [], events: [], held_units: 0};
    throw new Error('history unavailable');
  });
  f.render(); f.h.effects(); await flush();
  assert.equal(calls.some(path => path.startsWith('portal/requests')), false);
  assert.match(JSON.stringify(f.render()), /fixture/);
});

test('unreachable portal session is a retryable connection state, not a sign-in form', async () => {
  const f = portalPageFixture(async () => {throw new Error('synthetic offline');});
  f.render(); f.h.effects(); await flush();
  assert.equal(descendants(f.render()).some(node => node.type === 'login'), false);
  assert.match(JSON.stringify(f.render()), /重新连接/);
});

function workspaceFixture(fetch) {
  const h = hookHarness(); let heartbeat;
  class PortalError extends Error {constructor(status, message) {super(message); this.status = status;}}
  const {usePortalWorkspace} = load('lib/use-portal-workspace.ts', {
    react: h.react, './portal-api': {PortalError, portalFetch: fetch},
    './use-heartbeat': {useHeartbeat(fn) {heartbeat = fn;}}, globals: {AbortController},
  });
  const render = () => h.render(usePortalWorkspace);
  return {h, render, PortalError, heartbeat: () => heartbeat()};
}

test('workspace partial failure retains confirmed identity and never marks network errors as anonymous', async () => {
  let attempt = 0;
  const f = workspaceFixture(async path => {
    if (path === 'portal/me') return {id: 'fixture', username: 'fixture'};
    if (++attempt === 1) throw new Error('overview offline');
    return {balance_units: 55};
  });
  f.render(); f.h.effects(); await flush();
  let state = f.render(); assert.equal(state.me.id, 'fixture'); assert.equal(state.anonymous, false);
  assert.equal(state.data, null); assert.equal(state.pending, false); assert.match(state.error, /overview offline/);
  await state.reload(); state = f.render(); assert.equal(state.data.balance_units, 55); assert.equal(state.error, '');
});

test('workspace background reads coalesce, mutation reload supersedes old work, and reset rejects late data', async () => {
  const reads = [], calls = [];
  const f = workspaceFixture(path => {
    calls.push(path);
    if (path === 'portal/me') return Promise.resolve({id: 'fixture', username: 'fixture'});
    const pending = deferred(); reads.push(pending); return pending.promise;
  });
  f.render(); f.h.effects(); await flush();
  const background = f.heartbeat(); await flush(); assert.equal(reads.length, 1);
  const reload = f.render().reload(); await flush(); assert.equal(reads.length, 2);
  reads[1].resolve({balance_units: 99}); await reload;
  reads[0].resolve({balance_units: 1}); await background; await flush();
  assert.equal(f.render().data.balance_units, 99);
  const last = f.render().reload(); await flush(); f.render().reset(); reads[2].resolve({balance_units: 100}); await last;
  assert.equal(f.render().me, null); assert.equal(f.render().data, null); assert.equal(f.render().anonymous, true);
  assert.equal(calls.some(path => path.startsWith('portal/requests')), false);
});

test('workspace authorization revocation clears retained state and aborts unmounted reads', async () => {
  let rejected = false, f;
  f = workspaceFixture(async path => {
    if (rejected) throw new f.PortalError(403, 'session revoked');
    return path === 'portal/me' ? {id: 'fixture', username: 'fixture'} : {balance_units: 99};
  });
  f.render(); f.h.effects(); await flush(); assert.equal(f.render().data.balance_units, 99);
  rejected = true; await f.render().reload();
  assert.equal(f.render().anonymous, true); assert.equal(f.render().data, null); assert.match(f.render().error, /revoked/);
  const waiting = deferred(); let signal;
  const other = workspaceFixture((path, options) => {signal = options.signal; return waiting.promise;});
  other.render(); other.h.effects(); await flush(); other.h.unmount();
  assert.equal(signal.aborted, true); waiting.resolve({id: 'late'}); await flush(); assert.equal(other.render().me, null);
});

test('workspace login form remains mounted during anonymous background refresh and connect clears it deliberately', async () => {
  let f, authenticated = false;
  const pending = deferred();
  f = workspaceFixture(async path => {
    if (!authenticated) throw new f.PortalError(401,'anonymous');
    if (path === 'portal/me') return pending.promise;
    return {balance_units:9};
  });
  f.render(); f.h.effects(); await flush(); assert.equal(f.render().anonymous,true);
  authenticated=true;
  const work=f.heartbeat(); await flush();
  assert.equal(f.render().anonymous,true); assert.equal(f.render().pending,true);
  const connect=f.render().connect(); await flush(); assert.equal(f.render().anonymous,false);
  pending.resolve({id:'fixture',username:'fixture'}); await work;await connect;
  assert.equal(f.render().me.id,'fixture');assert.equal(f.render().data.balance_units,9);
});

test('workspace Strict Mode replay aborts first read and accepts replacement', async () => {
  const calls=[];
  const f=workspaceFixture((path,options)=>{
    const work=deferred(); calls.push({path,signal:options.signal,work});return work.promise;
  });
  f.render();f.h.effects();await flush();f.h.replayEffects();await flush();
  assert.equal(calls.length,2);assert.equal(calls[0].signal.aborted,true);
  calls[0].work.resolve({id:'old'});calls[1].work.resolve({id:'new'});await flush();
  assert.equal(calls[2].path,'portal/overview');calls[2].work.resolve({balance_units:22});await flush();
  assert.equal(f.render().me.id,'new');assert.equal(f.render().data.balance_units,22);
});

function requestFixture(fetch) {
  const h = hookHarness();
  const jsx = (type, props) => ({type, props});
  let revoked = 0, heartbeat;
  const unauthorized = () => {revoked++;};
  class PortalError extends Error {constructor(status, message) {super(message); this.status = status;}}
  const {default: Requests} = load('app/portal/PortalRequests.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'lucide-react': {History:'icon',RefreshCw:'icon',ChevronLeft:'icon',ChevronRight:'icon'},
    '@/lib/portal-api': {portalFetch: fetch, PortalError, portalTime: String},
    '@/lib/portal-display': {requestCredit: value => value === null ? '待核实' : String(value),requestLatency:String,
      requestTokens:(a,b)=>String(a+b),requestStatus:()=>({tone:'success',label:'成功'})},
    '@/lib/use-heartbeat': {useHeartbeat(fn) {heartbeat = fn;}}, globals: {AbortController},
  });
  const render = () => h.render(() => Requests({onUnauthorized: unauthorized}));
  const button = name => descendants(render()).find(node => node.type === 'button' && node.props.children === name);
  return {h, render, button, PortalError, revoked: () => revoked, heartbeat: () => heartbeat()};
}

test('request history stays page-bound, exposes load errors and retries without touching overview', async () => {
  const calls = []; let fail = false;
  const f = requestFixture(async path => {
    calls.push(path); if (fail) throw new Error('history offline');
    return {page: path.endsWith('2') ? 2 : 1, total: 35, items: [{ts: 1, model: path.endsWith('2') ? 'page-two' : 'page-one',status:200,prompt_tokens:3,completion_tokens:4,latency_ms:20,credit:null}]};
  });
  f.render(); f.h.effects(); await flush(); assert.match(JSON.stringify(f.render()), /page-one/);
  f.button('下一页').props.onClick(); f.render(); f.h.effects(); await flush();
  assert.match(JSON.stringify(f.render()), /page-two/); assert.doesNotMatch(JSON.stringify(f.render()), /page-one/);
  assert.equal(f.button('下一页').props.disabled, true);
  fail = true; await f.heartbeat();
  assert.match(JSON.stringify(f.render()), /history offline/); assert.match(JSON.stringify(f.render()), /上次读取/);
  fail = false; f.button('重试记录').props.onClick(); await flush(); assert.doesNotMatch(JSON.stringify(f.render()), /history offline/);
  assert.equal(calls.every(path => path.startsWith('portal/requests?page=')), true);
});

test('request history never renders a previous page under failed new-page or authorization state', async () => {
  let f, revoked = false;
  f = requestFixture(async path => {
    if (revoked) throw new f.PortalError(401, 'expired');
    if (path.endsWith('2')) throw new Error('page two unavailable');
    return {page:1,total:35,items:[{ts:1,model:'private-page-one',status:200,prompt_tokens:0,completion_tokens:0,latency_ms:0,credit:0}]};
  });
  f.render(); f.h.effects(); await flush(); f.button('下一页').props.onClick(); f.render(); f.h.effects(); await flush();
  assert.match(JSON.stringify(f.render()), /page two unavailable/); assert.doesNotMatch(JSON.stringify(f.render()), /private-page-one/);
  revoked = true; await f.heartbeat(); assert.equal(f.revoked(), 1); assert.doesNotMatch(JSON.stringify(f.render()), /private-page-one/);
});

function portalKeyFixture(fetch) {
  const h = hookHarness(); const jsx = (type, props) => ({type, props}); let revoked = 0;
  class PortalError extends Error {constructor(status, message) {super(message); this.status = status;}}
  const unauthorized = () => {revoked++;};
  const {default: Keys} = load('app/portal/PortalKeys.tsx', {react: h.react, 'react/jsx-runtime': {jsx,jsxs:jsx},
    '@/lib/base-path':{BASE_PATH:''},'@/lib/portal-api':{portalFetch:fetch,PortalError,portalTime:String},
    '@/components/animate-ui/radix/dialog':Object.fromEntries(['Dialog','DialogBody','DialogContent','DialogDescription','DialogHeader','DialogTitle'].map(name=>[name,name])),
    globals:{AbortController,window:{location:{origin:'http://localhost'}}},
  });
  const render=()=>h.render(()=>Keys({onUnauthorized:unauthorized}));
  return {h,render,PortalError,revoked:()=>revoked,form:()=>descendants(render()).find(node=>node.type==='form')};
}

test('portal key creation preserves one-time result and blocks duplicate creation if following list refresh fails', async () => {
  let reads=0,writes=0;
  const f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='POST'){writes++;return {id:7,key:'synthetic-one-time-value'};}
    if(++reads>1)throw new Error('list offline'); return {items:[],allowed_models:['deepseek-fixture']};
  });
  f.render();f.h.effects();await flush(); f.form().props.onSubmit({preventDefault(){}});await flush();
  const tree=JSON.stringify(f.render()); assert.match(tree,/synthetic-one-time-value/);assert.match(tree,/密钥已生成，但列表刷新失败/);
  f.form().props.onSubmit({preventDefault(){}}); await flush(); assert.equal(writes,1);
});

test('revoked portal key sessions clear any one-time secret and prevent late initial responses', async () => {
  let reads=0,f;
  f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='POST')return {id:7,key:'synthetic-private-value'};
    if(++reads>1)throw new f.PortalError(401,'revoked'); return {items:[],allowed_models:[]};
  });
  f.render();f.h.effects();await flush();f.form().props.onSubmit({preventDefault(){}});await flush();
  assert.equal(f.revoked(),1);assert.doesNotMatch(JSON.stringify(f.render()),/synthetic-private-value/);
});

test('uncertain portal key creation requires a list read before another mutation', async () => {
  let writes=0;
  const f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='POST'){writes++;throw new Error('synthetic connection lost');}
    return {items:[],allowed_models:[]};
  });
  f.render();f.h.effects();await flush();f.form().props.onSubmit({preventDefault(){}});await flush();
  assert.match(JSON.stringify(f.render()),/操作结果待确认/);
  f.form().props.onSubmit({preventDefault(){}});await flush();assert.equal(writes,1);
  descendants(f.render()).find(node=>node.type==='button'&&node.props.children==='刷新列表').props.onClick();await flush();
  f.form().props.onSubmit({preventDefault(){}});await flush();assert.equal(writes,2);
});

test('request page change aborts obsolete work and late response cannot replace the current page', async () => {
  const reads=[];
  const f=requestFixture((path,options)=>{
    const work=deferred();reads.push({path,signal:options.signal,work});return work.promise;
  });
  f.render();f.h.effects();await flush();
  reads[0].work.resolve({page:1,total:35,items:[{ts:1,model:'first',status:200,prompt_tokens:1,completion_tokens:2,latency_ms:5,credit:1}]});await flush();
  const old=f.heartbeat();await flush();f.button('下一页').props.onClick();f.render();f.h.effects();await flush();
  assert.equal(reads[1].signal.aborted,true);
  reads[2].work.resolve({page:2,total:35,items:[{ts:2,model:'current',status:200,prompt_tokens:1,completion_tokens:2,latency_ms:5,credit:1}]});await flush();
  reads[1].work.resolve({page:1,total:35,items:[{ts:1,model:'late-private',status:200,prompt_tokens:1,completion_tokens:2,latency_ms:5,credit:1}]});await old;
  assert.match(JSON.stringify(f.render()),/current/);assert.doesNotMatch(JSON.stringify(f.render()),/late-private/);
  const pending=f.heartbeat();await flush();f.h.unmount();assert.equal(reads[3].signal.aborted,true);reads[3].work.resolve({page:2,total:0,items:[]});await pending;
});

test('successful portal key revocation remains disabled even if its follow-up list read fails', async () => {
  let reads=0,deletes=0;
  const f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='DELETE'){deletes++;return {ok:true};}
    if(++reads>1)throw new Error('list unavailable');
    return {items:[{id:7,label:'fixture',prefix:'synthetic',enabled:true,created_at:1}],allowed_models:[]};
  });
  f.render();f.h.effects();await flush();
  descendants(f.render()).find(node=>node.type==='button'&&node.props.children==='撤销').props.onClick();
  descendants(f.render()).find(node=>node.type==='button'&&node.props.children==='确认撤销').props.onClick();await flush();
  assert.equal(deletes,1);assert.match(JSON.stringify(f.render()),/密钥已撤销，但列表刷新失败/);
  assert.equal(descendants(f.render()).find(node=>node.type==='button'&&node.props.children==='撤销').props.disabled,true);
  assert.doesNotMatch(JSON.stringify(f.render()),/确认撤销/);
});

test('portal action authorization failure immediately clears private workspace instead of retaining it', async () => {
  let f;
  f = portalPageFixture(async path => {
    if (path === 'portal/me') return {id: 'private-user', username: 'private-user'};
    if (path === 'portal/overview') return {accounts: [], grants: [], events: [], held_units: 0};
    throw new f.PortalError(401, 'session expired');
  });
  f.render(); f.h.effects(); await flush(); f.render(); f.h.effects();
  descendants(f.render()).find(node => node.type === 'button' && JSON.stringify(node.props.children).includes('扫码添加账号')).props.onClick();
  await flush();
  assert.equal(descendants(f.render()).some(node => node.type === 'login'), true);
  assert.doesNotMatch(JSON.stringify(f.render()), /private-user/);
});

test('uncertain portal actions require a confirmed read before another submission', async () => {
  let writes = 0;
  const f = portalPageFixture(async path => {
    if (path === 'portal/me') return {id: 'fixture', username: 'fixture'};
    if (path === 'portal/overview') return {accounts: [], grants: [], events: [], held_units: 0};
    writes++; throw new Error('synthetic connection lost');
  });
  f.render(); f.h.effects(); await flush(); f.render(); f.h.effects();
  const add = () => descendants(f.render()).find(node => node.type === 'button' && JSON.stringify(node.props.children).includes('扫码添加账号'));
  add().props.onClick(); await flush(); add().props.onClick(); await flush();
  assert.equal(writes, 1); assert.match(JSON.stringify(f.render()), /核对/);
  descendants(f.render()).find(node => node.type === 'button' && node.props.children === '重新读取').props.onClick(); await flush();
  add().props.onClick(); await flush(); assert.equal(writes, 2);
});

test('scan polling pauses on failure, retries the same session only on demand, and cleans up on close', async () => {
  let polls = 0, starts = 0, cancelled = 0;
  const f = portalPageFixture(async path => {
    if (path === 'portal/me') return {id: 'fixture', username: 'fixture'};
    if (path === 'portal/overview') return {accounts: [], grants: [], events: [], held_units: 0};
    if (path === 'portal/auth/start') {starts++; return {session: 'synthetic-session', auth_url: 'https://example.invalid', expires_in: 300};}
    if (path === 'portal/auth/poll') {polls++; if (polls === 1) throw new Error('synthetic offline'); return {state: 'waiting'};}
    if (path === 'portal/auth/cancel') {cancelled++; return {ok: true};}
    throw new Error('unexpected operation');
  });
  f.render(); f.h.effects(); await flush(); f.render(); f.h.effects();
  descendants(f.render()).find(node => node.type === 'button' && JSON.stringify(node.props.children).includes('扫码添加账号')).props.onClick();
  await flush(); f.render(); f.h.effects();
  const tick = [...f.timers.values()][0]; await tick();
  assert.match(JSON.stringify(f.render()), /已暂停轮询/);
  assert.equal(descendants(f.render()).some(node => node.props?.role === 'dialog'), true);
  await tick(); assert.equal(polls, 1);
  descendants(f.render()).find(node => node.type === 'button' && node.props.children === '重试当前二维码').props.onClick();
  await tick(); assert.equal(polls, 2); assert.equal(starts, 1);
  descendants(f.render()).find(node => node.type === 'button' && node.props.children === '关闭').props.onClick();
  await flush(); f.render(); f.h.effects();
  assert.equal(cancelled, 1); assert.equal(f.timers.size, 0);
});

test('key page distinguishes enabled key, model discovery and paid dispatch without exposing other users', async () => {
  const f = portalKeyFixture(async () => ({items: [{id: 7, label: 'synthetic-owned', prefix: 'synthetic', enabled: true, created_at: 1}],
    allowed_models: ['deepseek-fixture'], model_discovery_enabled: true, pool_enabled: false, dispatch_enabled: false,
    dispatch_note: 'synthetic dispatch reason'}));
  f.render(); f.h.effects(); await flush();
  const view = JSON.stringify(f.render());
  assert.match(view, /密钥已创建，不代表模型调用已开放/);
  assert.match(view, /可读取模型目录/);
  assert.match(view, /synthetic dispatch reason/);
  assert.match(view, /已启用/);
});

test('approved key page states wallet cap and does not show the unpaid-activation warning', async () => {
  const f = portalKeyFixture(async () => ({items: [], allowed_models: ['deepseek-fixture'],
    model_discovery_enabled: true, pool_enabled: true, dispatch_enabled: true, dispatch_note: 'approved'}));
  f.render(); f.h.effects(); await flush();
  const view = JSON.stringify(f.render());
  assert.doesNotMatch(view, /密钥已创建，不代表模型调用已开放/);
  assert.match(view, /余额为零拒绝新请求/);
  assert.match(view, /超出部分由平台承担/);
  assert.match(view, /不扣未来签到/);
});
