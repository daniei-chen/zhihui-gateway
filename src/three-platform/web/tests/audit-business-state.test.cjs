const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

// Execute the production components. The deterministic hook scheduler lets us
// settle real event handlers in hostile order without a browser or a provider.
const root = path.resolve(__dirname, '..');
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function hooks() {
  const slots = [];
  let cursor = 0, pending = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const react = {
    useState(initial) {
      const index = cursor++;
      slots[index] ??= {value: typeof initial === 'function' ? initial() : initial};
      return [slots[index].value, value => { slots[index].value = typeof value === 'function' ? value(slots[index].value) : value; }];
    },
    useRef(initial) { const index = cursor++; slots[index] ??= {current: initial}; return slots[index]; },
    useId() { const index = cursor++; return `fixture-${index}`; },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = {value: callback, deps};
      return slots[index].value;
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = {value: factory(), deps};
      return slots[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) pending.push(() => {
        slots[index]?.cleanup?.(); slots[index] = {deps, effect, cleanup: effect()};
      });
    },
  };
  return {
    react,
    render(fn) { cursor = 0; return fn(); },
    effects() { const work = pending; pending = []; work.forEach(fn => fn()); },
    unmount() { slots.forEach(slot => slot?.cleanup?.()); },
  };
}
const jsx = (type, props) => ({type, props});
const components = new Proxy({}, {get: (_target, key) => key});
function load(relative, mocks = {}) {
  const source = fs.readFileSync(path.join(root, relative), 'utf8');
  const code = ts.transpileModule(source, {fileName: relative, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const compiled = {exports: {}};
  const context = {module: compiled, exports: compiled.exports, console, Promise, Error, AbortController,
    require(name) {
      if (name in mocks) return mocks[name];
      if (name.startsWith('@/components/') || name === 'lucide-react') return components;
      throw new Error(`Unmocked dependency: ${name}`);
    }, ...mocks.globals};
  vm.runInNewContext(code, context, {filename: relative});
  return {...compiled.exports, inspect: name => vm.runInNewContext(name, context)};
}
function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== 'object') return [];
  return [tree, ...['children', 'actions', 'content', 'trigger'].flatMap(key => nodes(tree.props?.[key]))];
}
function common(h, overrides = {}) {
  const t = key => key;
  return {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    '@/lib/i18n/provider': {useT: () => t, useI18n: () => ({t, tp: t, locale: 'en'})},
    '@/lib/i18n/rich-text': {RichText: 'RichText'},
    '@/lib/toast': {notify: {ok() {}, err() {}, warn() {}}},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: true, role: 'admin', me: {username: 'admin'}})},
    '@/lib/utils': {cn: (...values) => values.filter(Boolean).join(' ')},
    '@/lib/format': {fmtDateTime: String, fmtNumber: String, fmtAgo: String},
    '@/lib/base-path': {BASE_PATH: '/fixture'},
    ...overrides,
  };
}
const find = (tree, type, predicate = () => true) => {
  const node = nodes(tree).find(node => node.type === type && predicate(node.props));
  assert.ok(node, `Expected ${type}`);
  return node.props;
};
const button = (tree, text) => find(tree, 'Button', props => JSON.stringify(props.children)?.includes(text));

test('successful confirmation clears the real password dialog and rejects same-turn duplicate clicks', async () => {
  const owner = hooks(), dialog = hooks(), saved = deferred(), calls = [];
  const api = {errText: String, settingsApi: {updateUser(name, body) { calls.push({name, body}); return saved.promise; }}};
  const {ResetPasswordDialog} = load('components/common/settings/ResetPasswordDialog.tsx', common(owner, {
    '@/lib/api': api,
    '@/lib/managed-user-password': load('lib/managed-user-password.ts'),
  }));
  const {ConfirmDialog} = load('components/common/layout/ConfirmDialog.tsx', common(dialog, {'@/lib/api': api}));
  const renderOwner = () => owner.render(() => ResetPasswordDialog({username: 'operator', trigger: 'reset'}));
  const renderDialog = () => dialog.render(() => ConfirmDialog(renderOwner().props));
  find(renderOwner(), 'Input').onChange({target: {value: 'synthetic-secret-password'}});
  find(renderDialog(), 'AlertDialog').onOpenChange(true);
  const confirm = find(renderDialog(), 'AlertDialogAction').onClick;
  const first = confirm({preventDefault() {}}), second = confirm({preventDefault() {}});
  assert.equal(calls.length, 1);
  assert.equal(find(renderOwner(), 'Input').value, 'synthetic-secret-password');
  saved.resolve({sessions_revoked: true}); await Promise.all([first, second]);
  assert.equal(find(renderOwner(), 'Input').value, '');
  assert.equal(find(renderDialog(), 'AlertDialog').open, false);
});

function modelMapFixture() {
  const h = hooks(), writes = [], notices = [];
  let admin = true;
  const state = {values: {cfg: {available: true}, users: []}, errors: {modelMap: new Error('offline')},
    isRefreshing: false, reload: async () => false};
  const loaded = load('app/(main)/settings/layout.tsx', common(h, {
    'next/dynamic': {default: () => 'DynamicPanel'}, 'next/navigation': {usePathname: () => '/settings/models'},
    '@/lib/i18n': {t: String, tp: String},
    '@/lib/display-prefs': {getExpiryDailyGroup: () => false},
    '@/lib/settings-tabs': load('lib/settings-tabs.ts'),
    '@/lib/platform-context': {usePlatform: () => ({platform: 'workbuddy'})},
    '@/lib/schedule-validation': {scheduleDelayError: () => ''},
    '@/lib/managed-user-password': load('lib/managed-user-password.ts'),
    '@/lib/use-async-data': {useAsyncAll: () => state},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: admin, me: {username: 'admin'}})},
    '@/lib/toast': {notify: {ok: value => notices.push(value), err: value => notices.push(value)}},
    '@/lib/api': {errText: String, upstreamApi: {models: async () => ({models: [{id: 'known-model'}]})}, settingsApi: {
      saveModelMap(body) { const request = {...deferred(), body}; writes.push(request); return request.promise; },
    }},
  }));
  const render = () => h.render(() => loaded.default({children: null}));
  const fill = () => {
    find(render(), 'Input', props => props.placeholder === 'gpt-4o-mini').onChange({target: {value: 'new-alias'}});
    find(render(), 'Select', props => props.value === '').onValueChange('known-model');
  };
  return {h, state, writes, notices, render, fill, loaded, setAdmin: value => { admin = value; }};
}

test('an unknown model map cannot be displayed as empty or sent as a destructive replacement', async () => {
  const f = modelMapFixture(); f.render(); f.h.effects(); await flush();
  f.fill();
  const tree = f.render();
  assert.ok(nodes(tree).some(node => node.type === 'LoadError'));
  assert.doesNotMatch(JSON.stringify(tree), /settings\.mapEmpty/);
  const add = button(tree, 'settings.mapAdd');
  assert.equal(add.disabled, true);
  await add.onClick();
  assert.equal(f.writes.length, 0);
  f.h.unmount();
});

test('model map writes preserve existing aliases, serialize replacement and retain a failed draft', async () => {
  const f = modelMapFixture();
  f.state.values.modelMap = {'existing-alias': 'existing-model'}; f.state.errors = {};
  f.render(); f.h.effects(); await flush(); f.fill();
  const add = button(f.render(), 'settings.mapAdd').onClick;
  const first = add(), duplicate = add();
  assert.equal(f.writes.length, 1);
  assert.equal(f.writes[0].body['existing-alias'], 'existing-model');
  assert.equal(f.writes[0].body['new-alias'], 'known-model');
  f.writes[0].reject(new Error('write unavailable')); await Promise.all([first, duplicate]);
  assert.equal(find(f.render(), 'Input', props => props.placeholder === 'gpt-4o-mini').value, 'new-alias');
  assert.equal(button(f.render(), 'settings.mapAdd').disabled, false);
  const retry = button(f.render(), 'settings.mapAdd').onClick();
  f.writes[1].resolve(f.writes[1].body); await retry;
  assert.equal(find(f.render(), 'Input', props => props.placeholder === 'gpt-4o-mini').value, '');
  assert.match(JSON.stringify(f.render()), /existing-alias/);
  f.state.values.modelMap = {'server-newer': 'new-model'};
  assert.match(JSON.stringify(f.render()), /server-newer/);
  assert.doesNotMatch(JSON.stringify(f.render()), /existing-alias/);
  f.h.unmount();
});

test('model map results from an obsolete permission scope cannot restore optimistic data', async () => {
  const f = modelMapFixture(); f.state.values.modelMap = {}; f.state.errors = {};
  f.render(); f.h.effects(); await flush(); f.fill();
  const saving = button(f.render(), 'settings.mapAdd').onClick();
  f.setAdmin(false); f.render(); f.h.effects();
  f.writes[0].resolve({'secret-old-alias': 'old-target'}); await saving;
  assert.doesNotMatch(JSON.stringify(f.render()), /secret-old-alias/);
  assert.equal(f.notices.length, 0);
  f.h.unmount();
});

test('duration input normalization uses empty values without inventing undefined or null strings', () => {
  const {loaded} = modelMapFixture();
  const pick = loaded.inspect('pickValues');
  for (const property of ['allowEmpty', 'offWhenZero']) {
    const fields = [{key: 'delay', kind: 'duration', def: '5m', [property]: true}];
    for (const source of [undefined, {}, {delay: null}]) assert.equal(pick(fields, source).delay, '');
    assert.equal(pick(fields, {delay: ' 30s '}).delay, '30s');
    assert.equal(pick(fields, {delay: 'broken'}).delay, '5m');
  }
});

function securityFixture() {
  const h = hooks(), writes = [], notices = [];
  let admin = true;
  const state = {values: {config: {enabled: false, mode: 'blacklist'}, rules: [], logs: [], audit: {items: []}},
    errors: {}, reload: async () => false};
  const {default: Security} = load('app/(main)/security/page.tsx', common(h, {
    '@/lib/use-heartbeat': {useHeartbeat() {}}, '@/lib/use-async-data': {useAsyncAll: () => state},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: admin, role: admin ? 'admin' : 'viewer', me: {username: 'admin'}})},
    '@/lib/toast': {notify: {ok: value => notices.push(value), err: value => notices.push(value)}},
    '@/lib/api': {errText: String, settingsApi: {}, securityApi: {
      saveConfig(body) {const request = {...deferred(), body}; writes.push(request); return request.promise;},
    }},
  }));
  return {h, state, writes, notices, render: () => h.render(Security), setAdmin: value => {admin = value;}};
}

test('security switch and mode cannot concurrently replace the same whole policy', async () => {
  const f = securityFixture(); f.render(); f.h.effects();
  const tree = f.render();
  const first = find(tree, 'Switch', props => props['aria-label'] === 'security.enableControl').onCheckedChange(true);
  const second = find(tree, 'Select', props => props.value === 'blacklist').onValueChange('whitelist');
  assert.equal(f.writes.length, 1);
  assert.equal(find(f.render(), 'Select', props => props.value === 'blacklist').disabled, true);
  f.writes[0].resolve({enabled: true, mode: 'blacklist'}); await Promise.all([first, second]);
  assert.equal(find(f.render(), 'Switch', props => props['aria-label'] === 'security.enableControl').checked, true);
  f.state.values.config = {enabled: true, mode: 'whitelist'};
  assert.equal(find(f.render(), 'Select', props => props.value === 'whitelist').disabled, false);
  f.h.unmount();
});

test('a late security save cannot alter a new read-only scope', async () => {
  const f = securityFixture(); f.render(); f.h.effects();
  const first = find(f.render(), 'Switch', props => props['aria-label'] === 'security.enableControl').onCheckedChange(true);
  f.setAdmin(false); f.render(); f.h.effects();
  f.writes[0].resolve({enabled: true, mode: 'blacklist'}); await first;
  assert.equal(find(f.render(), 'Switch', props => props['aria-label'] === 'security.enableControl').checked, false);
  assert.equal(f.notices.length, 0);
  f.h.unmount();
});

const proxySettings = {http_configured: true, http_proxy: 'http://user:***@saved.invalid',
  update_configured: true, update_proxy: 'http://user:***@updates.invalid', global_proxy_enabled: false,
  source: 'off', effective: '', env_update_proxy: ''};
const proxyRoutes = {routes: [{name: 'route-a', masked: 'socks5://route.invalid', weight: 100, enabled: true, bound: 0}],
  pool: {enabled: true, strategy: 'priority', strategies: ['priority'], interval_seconds: 60, routes_total: 1, routes_enabled: 1}};
function proxyFixture() {
  const h = hooks(), routes = [], settings = [], writes = [];
  const {ProxyPanel} = load('components/common/settings/ProxyPanel.tsx', common(h, {
    '@/lib/api': {errText: String, accountApi: {}, proxyApi: {
      routes() {const read = deferred(); routes.push(read); return read.promise;},
      settings() {const read = deferred(); settings.push(read); return read.promise;},
      saveSettings(body) {const request = {...deferred(), body}; writes.push(request); return request.promise;},
      probe: async () => ({ok: true, ms: 1}),
    }},
  }));
  const render = () => h.render(ProxyPanel);
  const settle = async (cfg = proxySettings) => {
    routes.at(-1).resolve(proxyRoutes); settings.at(-1).resolve(cfg); await flush();
  };
  return {h, render, routes, settings, writes, settle};
}

test('proxy partial initial failure exposes retry, never a writable blank fallback config', async () => {
  const f = proxyFixture(); f.render(); f.h.effects();
  f.routes[0].resolve(proxyRoutes); f.settings[0].reject(new Error('settings offline')); await flush();
  const error = find(f.render(), 'LoadError');
  assert.equal(nodes(f.render()).filter(node => node.type === 'Input').length, 0);
  const retry = error.onRetry(); await f.settle(); await retry;
  const tree = f.render();
  assert.equal(find(tree, 'Input', props => props.placeholder === 'settings.proxyGlobalPlaceholder').value, proxySettings.http_proxy);
  assert.equal(find(tree, 'Switch', props => props['aria-label'] === 'settings.proxyGlobalTitle').checked, false);
  const click = button(tree, 'common.save').onClick;
  click(); click();
  assert.equal(f.writes.length, 1);
  assert.equal(f.writes[0].body.update_proxy, proxySettings.update_proxy);
  assert.equal(f.writes[0].body.global_proxy_enabled, false);
  f.writes[0].resolve(proxySettings); await flush(); f.h.unmount();
});

test('proxy probing cannot erase an unsaved global draft or turn a read failure into a writable state', async () => {
  const f = proxyFixture(); f.render(); f.h.effects(); await f.settle();
  find(f.render(), 'Input', props => props.placeholder === 'settings.proxyGlobalPlaceholder')
    .onChange({target: {value: 'http://edited.invalid'}});
  find(f.render(), 'Button', props => props.title === 'settings.proxyTest').onClick(); await flush();
  await f.settle();
  assert.equal(find(f.render(), 'Input', props => props.placeholder === 'settings.proxyGlobalPlaceholder').value, 'http://edited.invalid');
  find(f.render(), 'Button', props => props.title === 'settings.proxyTest').onClick(); await flush();
  f.routes.at(-1).reject(new Error('route refresh offline')); f.settings.at(-1).resolve(proxySettings); await flush();
  assert.ok(nodes(f.render()).some(node => node.type === 'LoadError'));
  const save = button(f.render(), 'common.save');
  assert.equal(save.disabled, true); save.onClick(); assert.equal(f.writes.length, 0);
  f.h.unmount();
});

test('proxy save ignores completion after unmount', async () => {
  const f = proxyFixture(); f.render(); f.h.effects(); await f.settle();
  button(f.render(), 'common.save').onClick(); f.h.unmount();
  f.writes[0].resolve({...proxySettings, http_proxy: 'http://obsolete.invalid'}); await flush();
  assert.doesNotMatch(JSON.stringify(f.render()), /obsolete\.invalid/);
});

test('a proxy save callback captured before refresh cannot invalidate the refresh and strand loading', async () => {
  const f = proxyFixture(); f.render(); f.h.effects(); await f.settle();
  const staleSave = button(f.render(), 'common.save').onClick;
  find(f.render(), 'Button', props => props.title === 'settings.proxyTest').onClick(); await flush();
  staleSave(); assert.equal(f.writes.length, 0);
  assert.equal(find(f.render(), 'ConfirmDialog').confirmDisabled, true);
  await f.settle();
  assert.equal(button(f.render(), 'common.save').disabled, false);
  assert.equal(find(f.render(), 'ConfirmDialog').confirmDisabled, false);
  button(f.render(), 'common.save').onClick(); assert.equal(f.writes.length, 1);
  f.writes[0].resolve(proxySettings); await flush(); f.h.unmount();
});

function keysFixture() {
  const h = hooks(), exports = [], imports = [], statuses = [], creates = [];
  const state = {values: {keys: [], users: [], upstreams: {items: []}}, errors: {}, reload: async () => true, refresh: async () => true};
  const {default: Keys} = load('app/(main)/keys/page.tsx', common(h, {
    '@/lib/use-async-data': {useAsyncAll: () => state}, '@/lib/use-heartbeat': {useHeartbeat() {}},
    '@/lib/realm-context': {useRealm: () => ({realm: 'cn'})},
    '@/lib/platform-context': {usePlatform: () => ({platform: 'workbuddy'})},
    '@/lib/key-limit-form': load('lib/key-limit-form.ts'),
    '@/lib/api': {errText: String, settingsApi: {}, upstreamsApi: {}, keyApi: {
      async create(body) {creates.push(body); return {key: `synthetic-key-${creates.length}`};},
      exportConfig(body) {const request = {...deferred(), body}; exports.push(request); return request.promise;},
      importLocal(body) {const request = {...deferred(), body}; imports.push(request); return request.promise;},
      importLocalStatus() {const request = deferred(); statuses.push(request); return request.promise;},
    }},
    globals: {window: {location: {origin: 'https://panel.invalid'}}},
  }));
  const render = () => h.render(Keys);
  const issue = async () => {
    button(render(), 'keys.newKey').onClick();
    find(render(), 'Input', props => props.id === 'key-name').onChange({target: {value: 'fixture key'}});
    await button(render(), 'keys.create').onClick();
    render(); h.effects(); await flush();
  };
  const selectTarget = target => {
    find(render(), 'Select', props => ['ccswitch-claude', 'ccswitch-codex', 'zcode'].includes(props.value)).onValueChange(target);
    render(); h.effects();
  };
  return {h, state, render, issue, selectTarget, exports, imports, statuses, creates};
}

test('failed upstream directory blocks both creating and submitting a default-bound key', async () => {
  const f = keysFixture();
  delete f.state.values.upstreams; f.state.errors.upstreams = new Error('directory offline');
  f.render(); f.h.effects();
  const create = button(f.render(), 'keys.newKey'); assert.equal(create.disabled, true); create.onClick();
  find(f.render(), 'Input', props => props.id === 'key-name').onChange({target: {value: 'fixture'}});
  const submit = button(f.render(), 'keys.create'); assert.equal(submit.disabled, true); await submit.onClick();
  assert.equal(f.creates.length, 0); f.h.unmount();
});

test('export target changes drop old payloads and old finalizers cannot unlock a newer export', async () => {
  const f = keysFixture(); f.render(); f.h.effects(); await f.issue();
  const old = button(f.render(), 'keys.exportBtn').onClick();
  assert.equal(f.exports[0].body.app, 'claude');
  f.selectTarget('ccswitch-codex');
  const current = button(f.render(), 'keys.exportBtn').onClick();
  f.exports[0].resolve({client: 'ccswitch', settings_config: {wrong: 'obsolete-claude'}}); await old;
  assert.doesNotMatch(JSON.stringify(f.render()), /obsolete-claude/);
  assert.equal(nodes(f.render()).find(node => node.type === 'Button' && node.props.onClick && node.props.disabled
    && node.props.children === 'common.loading')?.props.disabled, true);
  f.exports[1].resolve({client: 'ccswitch', settings_config: {correct: 'current-codex'}}); await current;
  assert.match(JSON.stringify(f.render()), /current-codex/); f.h.unmount();
});

test('closing a one-time key dialog removes results and rejects late secret material in a new dialog', async () => {
  const f = keysFixture(); f.render(); f.h.effects(); await f.issue();
  const old = button(f.render(), 'keys.exportBtn').onClick();
  button(f.render(), 'keys.savedIt').onClick(); await f.issue();
  assert.equal(f.creates.length, 2);
  f.exports[0].resolve({client: 'ccswitch', settings_config: {token: 'synthetic-key-1'}}); await old;
  assert.doesNotMatch(JSON.stringify(f.render()), /synthetic-key-1/);
  assert.match(JSON.stringify(f.render()), /synthetic-key-2/); f.h.unmount();
});

test('local import keeps one real mutation in flight and never attributes its result to another target', async () => {
  const f = keysFixture(); f.render(); f.h.effects(); await f.issue();
  const ready = {enabled: true, local_caller: true, clients: [{client: 'ccswitch', importable: true}, {client: 'zcode', importable: true}]};
  f.statuses.at(-1).resolve(ready); await flush();
  const first = button(f.render(), 'keys.importBtn').onClick();
  f.selectTarget('zcode'); f.statuses.at(-1).resolve(ready); await flush();
  // Force the event even while disabled to verify the synchronous write gate.
  await button(f.render(), 'common.loading').onClick();
  assert.equal(f.imports.length, 1);
  f.imports[0].resolve({method: 'direct', client: 'ccswitch', backup: 'obsolete-backup'}); await first;
  assert.doesNotMatch(JSON.stringify(f.render()), /obsolete-backup/);
  assert.equal(button(f.render(), 'keys.importBtn').disabled, false);
  f.h.unmount();
});

const pgConfig = {enabled: true, host: 'db.invalid', port: 5432, dbname: 'fixture', user: 'fixture', password: '***',
  sslmode: 'prefer', interval_minutes: 15, keep_local_backup: true, last_export_at: 0, last_import_at: 0};
const pgStatus = (running, step = '') => ({running, kind: 'export', ok: running ? null : true, logs: [], step,
  started_at: 0, percent: 1, tables_done: 0, tables_total: 1, rows: 0});
function pgFixture() {
  const h = hooks(), configs = [], statuses = [], runs = [], timers = new Map(), listeners = new Map();
  let now = 0, timerId = 0;
  const document = {hidden: false, addEventListener(name, callback) {listeners.set(name, callback);},
    removeEventListener(name, callback) {if (listeners.get(name) === callback) listeners.delete(name);}};
  const window = {setTimeout(callback, delay) {const id = ++timerId; timers.set(id, {callback, at: now + delay, delay}); return id;},
    clearTimeout(id) {timers.delete(id);}};
  const {PgSyncPanel} = load('components/common/settings/PgSyncPanel.tsx', common(h, {
    '@/lib/api': {errText: String, pgSyncApi: {
      config(signal) {const read = {...deferred(), signal}; configs.push(read); return read.promise;},
      status(signal) {const read = {...deferred(), signal}; statuses.push(read); return read.promise;},
      exportData() {const run = deferred(); runs.push(run); return run.promise;},
    }}, globals: {window, document},
  }));
  const render = () => h.render(PgSyncPanel);
  const settleConfig = async (running = false) => {
    configs.at(-1).resolve({config: pgConfig, status: pgStatus(running)}); await flush(); render(); h.effects();
  };
  const advance = async ms => {
    const target = now + ms;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
    }
    now = target;
  };
  const hide = hidden => {document.hidden = hidden; listeners.get('visibilitychange')?.();};
  return {h, render, configs, statuses, runs, timers, listeners, settleConfig, advance, hide};
}

test('backup polling has one in-flight read, pauses hidden tabs and rejects canceled late progress', async () => {
  const f = pgFixture(); f.render(); f.h.effects(); await f.settleConfig(true);
  await f.advance(1500); assert.equal(f.statuses.length, 1);
  await f.advance(60000); assert.equal(f.statuses.length, 1);
  assert.equal(f.timers.size, 0);
  f.hide(true); assert.equal(f.statuses[0].signal.aborted, true);
  f.statuses[0].resolve(pgStatus(false, 'obsolete-hidden-complete')); await flush();
  assert.doesNotMatch(JSON.stringify(f.render()), /obsolete-hidden-complete/);
  assert.equal(f.timers.size, 0);
  f.hide(false); assert.equal(f.statuses.length, 2);
  f.statuses[1].resolve(pgStatus(true, 'current-running')); await flush();
  assert.match(JSON.stringify(f.render()), /current-running/);
  assert.deepEqual([...f.timers.values()].map(timer => timer.delay), [1500]);
  f.h.unmount(); assert.equal(f.timers.size, 0); assert.equal(f.listeners.size, 0);
});

test('starting backup invalidates an earlier idle poll without allowing duplicate actions', async () => {
  const f = pgFixture(); f.render(); f.h.effects(); await f.settleConfig();
  await f.advance(15000); assert.equal(f.statuses.length, 1);
  const run = find(f.render(), 'ConfirmDialog', props => props.title === 'pgSync.exportConfirmTitle').onConfirm;
  const first = run(), duplicate = run(); assert.equal(f.runs.length, 1);
  assert.equal(f.statuses[0].signal.aborted, true);
  f.runs[0].resolve({ok: true, status: pgStatus(true, 'accepted-new-run')}); await Promise.all([first, duplicate]);
  f.render(); f.h.effects();
  f.statuses[0].resolve(pgStatus(false, 'obsolete-idle')); await flush();
  const tree = JSON.stringify(f.render());
  assert.match(tree, /accepted-new-run/); assert.doesNotMatch(tree, /obsolete-idle/);
  assert.equal([...f.timers.values()][0].delay, 1500);
  f.h.unmount();
});

test('backup unmount aborts configuration and progress reads and never schedules retries', async () => {
  const f = pgFixture(); f.render(); f.h.effects();
  f.h.unmount(); assert.equal(f.configs[0].signal.aborted, true);
  f.configs[0].resolve({config: {...pgConfig, host: 'obsolete-db.invalid'}, status: pgStatus(true)}); await flush();
  assert.doesNotMatch(JSON.stringify(f.render()), /obsolete-db/); assert.equal(f.timers.size, 0);
  const ready = pgFixture(); ready.render(); ready.h.effects(); await ready.settleConfig();
  await ready.advance(15000); ready.h.unmount(); assert.equal(ready.statuses[0].signal.aborted, true);
  ready.statuses[0].resolve(pgStatus(true, 'obsolete-after-unmount')); await flush();
  assert.equal(ready.timers.size, 0);
  assert.doesNotMatch(JSON.stringify(ready.render()), /obsolete-after-unmount/);
});

test('backup does not treat a status invalidated while hidden as proof of an idle job', async () => {
  const f = pgFixture(); f.render(); f.h.effects(); f.hide(true);
  await f.settleConfig(true);
  const beforeResume = find(f.render(), 'ConfirmDialog', props => props.title === 'pgSync.exportConfirmTitle');
  assert.equal(beforeResume.confirmDisabled, true);
  await beforeResume.onConfirm(); assert.equal(f.runs.length, 0);
  f.hide(false); assert.equal(f.statuses.length, 1);
  await find(f.render(), 'ConfirmDialog', props => props.title === 'pgSync.exportConfirmTitle').onConfirm();
  assert.equal(f.runs.length, 0);
  f.statuses[0].resolve(pgStatus(true, 'confirmed-running')); await flush(); f.render(); f.h.effects();
  assert.equal(find(f.render(), 'ConfirmDialog', props => props.title === 'pgSync.exportConfirmTitle').confirmDisabled, true);
  f.h.unmount();
});
