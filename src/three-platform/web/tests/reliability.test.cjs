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

function asyncHook() {
  const harness = hookHarness();
  const state = load('lib/async-state.ts', {});
  const {useAsyncAll} = load('lib/use-async-data.ts', {react: harness.react, '@/lib/async-state': state});
  return {harness, useAsyncAll};
}

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
    './platform-store': {platformStore: {explicit: () => explicit}}, globals: {window, URLSearchParams}});
  for (const params of [undefined, {}, {platform: undefined}]) {
    const config = requestInterceptor({headers: {}, params});
    assert.equal(config.headers['X-QTG-Platform'], 'qoder'); assert.equal(config.params.platform, 'qoder');
  }
  const override = requestInterceptor({headers: {}, params: {platform: 'trae', page: 2}});
  assert.equal(override.params.platform, 'trae'); assert.equal(override.params.page, 2);
  const encoded = requestInterceptor({headers: {}, params: new URLSearchParams('realm=global')});
  assert.equal(encoded.params.get('platform'), 'qoder'); assert.equal(encoded.params.get('realm'), 'global');
  explicit = null;
  assert.equal(requestInterceptor({headers: {}}).params, undefined);
  await assert.rejects(responseError({response: {status: 401}}));
  assert.equal(window.location.href, '/login');
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

test('backend error details remain authoritative and server diagnostics get localized context', () => {
  const {errText} = load('lib/api.ts', {
    axios: {default: {create: () => ({interceptors: {
      request: {use() {}}, response: {use() {}},
    }})}},
    './base-path': {BASE_PATH: ''},
    './err-envelope': {backendErrorText: body => typeof body?.detail === 'string' ? body.detail : ''},
    './i18n': {t: (key, params) => params ? `${key}: ${params.detail}` : key, tp: text => text},
    './platform-store': {},
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
