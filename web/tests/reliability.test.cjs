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
    useId() { const index = cursor++; slots[index] ??= {value: `test-id-${index}`}; return slots[index].value; },
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
    require(name) {
      if (name === './account-export.mjs') return require('../lib/account-export.mjs');
      if (!(name in mocks)) throw new Error(`Unexpected dependency ${name}`);
      return mocks[name];
    },
    ...mocks.globals,
  };
  vm.runInNewContext(code, context, {filename: relative});
  return compiled.exports;
}

function reviewPanelFixture() {
  const harness = hookHarness();
  let admin = true;
  const reads = [], archives = [], evidence = [];
  const jsx = (type, props) => ({type, props});
  const {BudgetReviewPanel} = load('components/common/settings/BudgetReviewPanel.tsx', {
    react: harness.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': {RefreshCw: 'icon'}, '@/components/ui/button': {Button: 'button'},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: admin})},
    '@/lib/i18n/provider': {useI18n: () => ({t: key => key, locale: 'en'})},
    '@/lib/api': {errText: () => 'synthetic unavailable'},
    '@/lib/operational-api': {operationalApi: {
      budgetReview(bucket, cursor, signal) { const entry = {...deferred(), bucket, cursor, signal}; reads.push(entry); return entry.promise; },
      retention(signal) { const entry = {...deferred(), signal}; archives.push(entry); return entry.promise; },
      budgetEvidence(requestId, signal) { const entry = {...deferred(), requestId, signal}; evidence.push(entry); return entry.promise; },
    }}, globals: {AbortController},
  });
  return {harness, reads, archives, evidence, setAdmin: value => {admin = value;}, render: () => harness.render(BudgetReviewPanel)};
}

function reviewNodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(reviewNodes);
  if (!tree || typeof tree !== 'object') return [];
  return [tree, ...reviewNodes(tree.props?.children)];
}

function reviewPage(requestId) {
  return {items: [{request_id: requestId, key_id: 1, created_at: 1000, class: 'unexplained'}], next_cursor: null};
}

function releasePanelFixture() {
  const harness = hookHarness();
  const versions = deferred(), status = deferred(), check = deferred();
  const calls = [];
  const jsx = (type, props) => ({type, props});
  const {UpdatePanel} = load('components/common/settings/UpdatePanel.tsx', {
    react: harness.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': new Proxy({}, {get: (_target, key) => key}),
    '@/lib/api': {errText: String, systemApi: {
      versions() {calls.push('versions'); return versions.promise;},
      updateStatus() {calls.push('status'); return status.promise;},
      checkUpdate() {calls.push('check'); return check.promise;},
    }},
    '@/lib/product': {PRODUCT_NAME: '智汇网关', PRODUCT_REPOSITORY: 'synthetic/zhihui', PRODUCT_REPOSITORY_URL: 'https://github.com/synthetic/zhihui'},
    '@/lib/toast': {notify: {}}, '@/lib/format': {fmtDateTime: String},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: false})},
    '@/components/ui/button': {Button: 'button'}, '@/components/ui/badge': {Badge: 'span'},
    '@/components/ui/input': {Input: 'input'}, '@/components/ui/label': {Label: 'label'},
    '@/components/common/layout/ConfirmDialog': {ConfirmDialog: 'confirm'},
    '@/components/common/states/LoadError': {LoadError: 'load-error'},
    '@/lib/i18n/provider': {useT: () => key => key},
    globals: {window: {setInterval() {throw new Error('release center must not poll');}}},
  });
  return {harness, versions, status, check, calls, render: () => harness.render(UpdatePanel)};
}

test('release center local metadata is not held behind a slow GitHub check and does not poll', async () => {
  const fixture = releasePanelFixture();
  fixture.render(); fixture.harness.effects();
  const product = {repository: 'synthetic/zhihui', version: '0.0.7'};
  fixture.versions.resolve({product}); fixture.status.resolve({product, record_state: 'absent'});
  await flush();
  const text = JSON.stringify(fixture.render());
  assert.match(text, /0\.0\.7/);
  assert.match(text, /releaseCenter\.checking/);
  assert.deepEqual(fixture.calls, ['versions', 'status', 'check']);
  fixture.harness.unmount();
  fixture.check.resolve({manager: {repo: 'synthetic/zhihui', has_update: true, latest: 'v99.0.0'}});
  await flush();
  assert.doesNotMatch(JSON.stringify(fixture.render()), /v99\.0\.0/);
});

test('release center preserves independent good metadata but reports incomplete loads', async () => {
  const fixture = releasePanelFixture();
  fixture.render(); fixture.harness.effects();
  fixture.versions.reject(new Error('synthetic unavailable'));
  fixture.status.resolve({product: {repository: 'synthetic/zhihui', version: '0.0.7'}, record_state: 'absent'});
  fixture.check.resolve({manager: {repo: 'foreign/project', has_update: true, latest: 'v1.0.79'}});
  await flush();
  const text = JSON.stringify(fixture.render());
  assert.match(text, /0\.0\.7/);
  assert.match(text, /releaseCenter\.loadFailed/);
  assert.match(text, /releaseCenter\.checkUnknown/);
  assert.doesNotMatch(text, /v1\.0\.79/);
  fixture.harness.unmount();
});

test('release checks never turn stale/error metadata into up-to-date or pending assets into an installer', async () => {
  for (const manager of [{repo: 'synthetic/zhihui', has_update: false, stale: true, error: 'synthetic 403'},
    {repo: 'synthetic/zhihui', has_update: true, latest: 'v0.0.8', assets_ready: false}]) {
    const fixture = releasePanelFixture();
    fixture.render(); fixture.harness.effects();
    const product = {repository: 'synthetic/zhihui', version: '0.0.7'};
    fixture.versions.resolve({product}); fixture.status.resolve({product, record_state: 'absent'});
    fixture.check.resolve({manager}); await flush();
    const tree = fixture.render();
    const text = JSON.stringify(tree);
    assert.doesNotMatch(text, /releaseCenter\.noNewRelease/);
    assert.match(text, manager.error ? /releaseCenter\.checkUnknown/ : /releaseCenter\.assetsPending/);
    assert.equal(reviewNodes(tree).some(node => node.props?.['data-slot'] === 'update-target'), false);
    fixture.harness.unmount();
  }
});

test('budget review rejects late reads after filter change and never reads for non-admins', async () => {
  const fixture = reviewPanelFixture();
  fixture.setAdmin(false);
  assert.equal(fixture.render(), null);
  fixture.harness.effects();
  assert.equal(fixture.reads.length, 0);
  fixture.setAdmin(true);
  let tree = fixture.render(); fixture.harness.effects();
  const select = reviewNodes(tree).find(node => node.type === 'select');
  select.props.onChange({target: {value: 'older_7d'}});
  fixture.render(); fixture.harness.effects();
  assert.equal(fixture.reads[0].signal.aborted, true);
  fixture.reads[0].resolve(reviewPage('synthetic-old'));
  fixture.archives[0].resolve({enabled: true, sampled: true, ok: true, fresh: true});
  fixture.reads[1].resolve(reviewPage('synthetic-new'));
  fixture.archives[1].resolve({enabled: true, sampled: true, ok: true, fresh: true});
  await flush();
  tree = fixture.render();
  assert.ok(JSON.stringify(tree).includes('synthetic-new'));
  assert.ok(!JSON.stringify(tree).includes('synthetic-old'));
  fixture.setAdmin(false); assert.equal(fixture.render(), null); fixture.harness.effects();
  assert.equal(fixture.reads[1].signal.aborted, true);
  fixture.harness.unmount();
});

test('budget review preserves independent successful page and clears evidence on authorization denial', async () => {
  const fixture = reviewPanelFixture();
  fixture.render(); fixture.harness.effects();
  fixture.reads[0].resolve(reviewPage('synthetic-private'));
  fixture.archives[0].reject(new Error('synthetic archive unavailable'));
  await flush();
  let tree = fixture.render();
  assert.ok(JSON.stringify(tree).includes('synthetic-private'));
  assert.ok(JSON.stringify(tree).includes('budgetReview.archiveUnknown'));
  const button = reviewNodes(tree).find(node => node.type === 'button' && node.props.children === 'budgetReview.view');
  button.props.onClick();
  fixture.evidence[0].reject({response: {status: 403}});
  await flush();
  tree = fixture.render();
  assert.ok(!JSON.stringify(tree).includes('synthetic-private'));
  assert.ok(JSON.stringify(tree).includes('synthetic unavailable'));
  fixture.harness.unmount();
});

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

test('user password reset applies the panel policy, preserves raw input and clears on close', async () => {
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
      for (const value of ['123456789012', '1234567890123', 'long-mixed-password',
        'panel-unicode-password!', '\u{1f600}'.repeat(12)]) {
        input(tree).props.onChange({target: {value}});
        tree = render();
        assert.equal(input(tree).props.value, value);
        assert.equal(tree.props.confirmDisabled, false);
      }
      input(tree).props.onChange({target: {value: '\u{1f600}'.repeat(7)}});
      tree = render();
      assert.equal(tree.props.confirmDisabled, true);
    }
    input(tree).props.onChange({target: {value: username === 'admin' ? 'Root-Password!' : 'Panel-user-password!'}});
    tree = render();
    assert.equal(tree.props.confirmDisabled, false);
    await tree.props.onConfirm();
    assert.deepEqual(JSON.parse(JSON.stringify(writes[0])), {name: username, payload: {password: username === 'admin' ? 'Root-Password!' : 'Panel-user-password!'}});
    tree.props.onOpenChange(false);
    assert.equal(input(render()).props.value, '');
  }
});

test('five languages explain panel password minimum and translate the backend rejection', () => {
  const phrase = '密码至少 12 位';
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const dict = JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json'), 'utf8'));
    for (const key of ['managedPasswordHint', 'managedPasswordPlaceholder', 'resetManagedPasswordDesc']) {
       assert.match(dict.settings[key], /12/, locale + ':' + key);
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
    '@/lib/api': {authApi}, '@/lib/base-path': {BASE_PATH: ''}, globals: {window, AbortController},
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
    './PortalRegister': {default: 'registration-form'},
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

function portalPageFixture(fetch, extraGlobals = {}) {
  const h = hookHarness();
  const timers = new Map(); let timerId = 0;
  const jsx = (type, props) => ({type, props});
  class PortalError extends Error { constructor(status, message) { super(message); this.status = status; } }
  const api = {portalFetch: fetch, PortalError, points: String, portalTime: String, canCheckin: () => false, checkinLabel: String};
  const globals = {AbortController, URL, document: {hidden: false, activeElement: null, getElementById: () => null, addEventListener() {}, removeEventListener() {}},
    window: {setInterval(fn) {timers.set(++timerId, fn); return timerId;}, clearInterval(id) {timers.delete(id);}}, ...extraGlobals};
  const workspace = load('lib/use-portal-workspace.ts', {react: h.react,
    './portal-api': api, './use-heartbeat': {useHeartbeat() {}}, globals});
  const {default: Portal} = load('app/portal/page.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'qrcode.react': {QRCodeSVG: 'qr'},
    'lucide-react': {}, '@/lib/portal-api': api, './portal.css': {},
    './PortalKeys': {default: 'keys'}, './PortalLogin': {default: 'login'}, './PortalRequests': {default: 'requests'},
    '@/lib/use-portal-workspace': workspace, '@/lib/portal-display': {checkinSummary: () => ({total: 0,ready: 0,pending: 0,confirmed: 0})},
    '@/components/common/layout/BrandHome': {BrandHome: 'brand'}, '@/components/common/layout/StatCard': {StatCard: 'stat'},
    '@/components/common/layout/ConsoleAtmosphere': {ConsoleAtmosphere: 'atmosphere'},
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
    if (path === 'portal/workspace') return workspacePacket({accounts: [], grants: [], events: [], held_units: 0});
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

function workspacePacket(data = {}, id = 'fixture') {
  return {me: {id, username: id}, data: {owner_id: id, ...data}};
}

test('workspace failed snapshot is retryable without inventing an identity', async () => {
  let attempt = 0; const calls=[];
  const f = workspaceFixture(async path => {
    calls.push(path); if (++attempt === 1) throw new Error('snapshot offline');
    return workspacePacket({balance_units: 55});
  });
  f.render(); f.h.effects(); await flush();
  let state = f.render(); assert.equal(state.me, null); assert.equal(state.anonymous, false);
  assert.equal(state.data, null); assert.equal(state.pending, false); assert.match(state.error, /snapshot offline/);
  await state.reload(); state = f.render(); assert.equal(state.me.id,'fixture');assert.equal(state.data.balance_units,55);
  assert.equal(state.error,''); assert.deepEqual(calls,['portal/workspace','portal/workspace']);
});

test('workspace background reads coalesce and atomic B snapshot defeats late A', async () => {
  const reads = [], calls = [];
  const f = workspaceFixture(path => {calls.push(path);const pending=deferred();reads.push(pending);return pending.promise;});
  f.render(); f.h.effects(); await flush();
  const background=f.heartbeat();await flush();assert.equal(reads.length,1);
  const reload=f.render().reload();await flush();assert.equal(reads.length,2);
  reads[1].resolve(workspacePacket({balance_units:99},'B'));await reload;
  reads[0].resolve(workspacePacket({balance_units:1},'A'));await background;await flush();
  assert.equal(f.render().me.id,'B');assert.equal(f.render().data.owner_id,'B');assert.equal(f.render().data.balance_units,99);
  const heartbeat=f.heartbeat();await flush();assert.equal(f.render().pending,false);
  reads[2].resolve(workspacePacket({balance_units:101},'B'));await heartbeat;
  const last=f.render().reload();await flush();f.render().reset();reads[3].resolve(workspacePacket({balance_units:100},'C'));await last;
  assert.equal(f.render().me,null);assert.equal(f.render().data,null);assert.equal(f.render().anonymous,true);
  assert.equal(calls.every(path=>path==='portal/workspace'),true);
});

test('workspace rejects a response combining two different owners', async () => {
  const f=workspaceFixture(async()=>({me:{id:'A',username:'A'},data:{owner_id:'B',balance_units:100}}));
  f.render();f.h.effects();await flush();
  assert.equal(f.render().data,null);assert.equal(f.render().me,null);assert.match(f.render().error,/身份不一致/);
});

test('workspace authorization revocation clears retained state and aborts unmounted reads', async () => {
  let rejected=false,f;
  f=workspaceFixture(async()=>{if(rejected)throw new f.PortalError(403,'session revoked');return workspacePacket({balance_units:99});});
  f.render();f.h.effects();await flush();assert.equal(f.render().data.balance_units,99);
  rejected=true;await f.render().reload();
  assert.equal(f.render().anonymous,true);assert.equal(f.render().data,null);assert.match(f.render().error,/revoked/);
  const waiting=deferred();let signal;
  const other=workspaceFixture((path,options)=>{signal=options.signal;return waiting.promise;});
  other.render();other.h.effects();await flush();other.h.unmount();
  assert.equal(signal.aborted,true);waiting.resolve(workspacePacket({},'late'));await flush();assert.equal(other.render().me,null);
});

test('workspace login form remains until connect explicitly changes state', async () => {
  let f,authenticated=false;const pending=deferred();
  f=workspaceFixture(async()=>{if(!authenticated)throw new f.PortalError(401,'anonymous');return pending.promise;});
  f.render();f.h.effects();await flush();assert.equal(f.render().anonymous,true);
  authenticated=true;const work=f.heartbeat();await flush();
  assert.equal(f.render().anonymous,true);assert.equal(f.render().pending,true);
  const connect=f.render().connect();await flush();assert.equal(f.render().anonymous,false);
  pending.resolve(workspacePacket({balance_units:9}));await work;await connect;
  assert.equal(f.render().me.id,'fixture');assert.equal(f.render().data.balance_units,9);
});

test('workspace Strict Mode replay replaces the entire owned snapshot', async () => {
  const calls=[];const f=workspaceFixture((path,options)=>{const work=deferred();calls.push({path,signal:options.signal,work});return work.promise;});
  f.render();f.h.effects();await flush();f.h.replayEffects();await flush();
  assert.equal(calls.length,2);assert.equal(calls[0].signal.aborted,true);
  calls[0].work.resolve(workspacePacket({},'old'));calls[1].work.resolve(workspacePacket({balance_units:22},'new'));await flush();
  assert.equal(calls.length,2);assert.equal(calls.every(call=>call.path==='portal/workspace'),true);
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

function portalRegisterFixture(fetch) {
  const h=hookHarness(),jsx=(type,props)=>({type,props});let registered='',cancelled=0;
  class PortalError extends Error {constructor(status,message){super(message);this.status=status;}}
  const {default:Register}=load('app/portal/PortalRegister.tsx',{
    react:h.react,'react/jsx-runtime':{jsx,jsxs:jsx},'@/lib/portal-api':{portalFetch:fetch,PortalError},
    '@/components/ui/button':{Button:'button'},'@/components/ui/input':{Input:'input'},'@/components/ui/label':{Label:'label'},globals:{AbortController},
  });
  const props={onRegistered:name=>{registered=name;},onCancel:()=>{cancelled++;}};
  const render=()=>h.render(()=>Register(props));
  const form=(values={})=>({elements:{namedItem:name=>({value:({username:'alice',password:'Cobalt!river_Sample904',confirmation:'Cobalt!river_Sample904',invite:'synthetic-invite',...values})[name]})},reset(){}});
  return {h,render,form,PortalError,registered:()=>registered,cancelled:()=>cancelled,submit:values=>descendants(render()).find(node=>node.type==='form').props.onSubmit({preventDefault(){},currentTarget:form(values)})};
}

test('signup server failure and used-device conflict block retries without depending on error wording',async()=>{
  for(const status of [500,503,409,400]){
    let writes=0;const f=portalRegisterFixture(async(path,options={})=>{
      if(options.method==='POST'){writes++;throw new f.PortalError(status,'synthetic opaque failure');}
      return {enabled:true,invite_required:true,device_registered:false};
    });
    f.render();f.h.effects();await flush();f.submit();await flush();f.submit();await flush();
    assert.equal(writes,status===400?2:1);
    if(status!==400)assert.match(JSON.stringify(f.render()),/结果待确认/);
  }
});

test('registration waits for device policy and prevents duplicate submits',async()=>{
  let writes=0;const pending=deferred();
  const f=portalRegisterFixture(async(path,options={})=>{
    if(options.method==='POST'){writes++;return pending.promise;}
    return {enabled:true,invite_required:true,device_registered:false};
  });
  f.render();f.h.effects();await flush();
  f.submit();f.submit();await flush();assert.equal(writes,1);
  pending.resolve({registered:true,username:'alice'});await flush();assert.equal(f.registered(),'alice');
});

test('registered devices and mismatching passwords cannot post a registration',async()=>{
  for(const registered of [true,false]){
    let writes=0;const f=portalRegisterFixture(async(path,options={})=>{
      if(options.method==='POST')writes++;
      return {enabled:true,invite_required:true,device_registered:registered};
    });
    f.render();f.h.effects();await flush();f.submit({confirmation:'different'});await flush();assert.equal(writes,0);
  }
});

test('uncertain signup never automatically retries and late completion cannot switch an unmounted page',async()=>{
  let writes=0;const f=portalRegisterFixture(async(path,options={})=>{
    if(options.method==='POST'){writes++;throw new Error('操作结果待确认，请核对');}
    return {enabled:true,invite_required:true,device_registered:false};
  });
  f.render();f.h.effects();await flush();f.submit();await flush();f.submit();await flush();assert.equal(writes,1);
  assert.match(JSON.stringify(f.render()),/结果待确认/);
  const pending=deferred();const g=portalRegisterFixture(async(path,options={})=>options.method==='POST'?pending.promise:{enabled:true,invite_required:true,device_registered:false});
  g.render();g.h.effects();await flush();g.submit();g.h.unmount();pending.resolve({registered:true,username:'late'});await flush();assert.equal(g.registered(),'');
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

test('existing default key displays username and removes all key creation controls',async()=>{
  let writes=0;
  const f=portalKeyFixture(async(path,options={})=>{
    if(options.method==='POST')writes++;
    return {items:[{id:7,label:'alice',prefix:'synthetic-prefix',enabled:true,created_at:1}],can_initialize:false,allowed_models:[]};
  });
  f.render();f.h.effects();await flush();assert.equal(f.form(),undefined);
  const tree=JSON.stringify(f.render());assert.match(tree,/alice/);assert.match(tree,/默认密钥/);assert.equal(writes,0);
  assert.doesNotMatch(tree,/最多.*5|生成密钥|密钥名称/);
});

test('portal key creation preserves one-time result and blocks duplicate creation if following list refresh fails', async () => {
  let reads=0,writes=0;
  const f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='POST'){writes++;return {id:7,key:'synthetic-one-time-value'};}
    if(++reads>1)throw new Error('list offline'); return {items:[],can_initialize:true,allowed_models:['deepseek-fixture']};
  });
  f.render();f.h.effects();await flush(); f.form().props.onSubmit({preventDefault(){}});await flush();
  const tree=JSON.stringify(f.render()); assert.match(tree,/synthetic-one-time-value/);assert.match(tree,/默认密钥已就绪，但列表刷新失败/);
  f.form().props.onSubmit({preventDefault(){}}); await flush(); assert.equal(writes,1);
});

test('revoked portal key sessions clear any one-time secret and prevent late initial responses', async () => {
  let reads=0,f;
  f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='POST')return {id:7,key:'synthetic-private-value'};
    if(++reads>1)throw new f.PortalError(401,'revoked'); return {items:[],can_initialize:true,allowed_models:[]};
  });
  f.render();f.h.effects();await flush();f.form().props.onSubmit({preventDefault(){}});await flush();
  assert.equal(f.revoked(),1);assert.doesNotMatch(JSON.stringify(f.render()),/synthetic-private-value/);
});

test('uncertain portal key creation requires a list read before another mutation', async () => {
  let writes=0;
  const f=portalKeyFixture(async (path,options={})=>{
    if(options.method==='POST'){writes++;throw new Error('synthetic connection lost');}
    return {items:[],can_initialize:true,allowed_models:[]};
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
    if (path === 'portal/workspace') return workspacePacket({accounts: [], grants: [], events: [], held_units: 0},'private-user');
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
    if (path === 'portal/workspace') return workspacePacket({accounts: [], grants: [], events: [], held_units: 0});
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
    if (path === 'portal/workspace') return workspacePacket({accounts: [], grants: [], events: [], held_units: 0});
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

test('QR expiry waits for an in-flight import and never starts another session', async () => {
  let now=0,polls=0,starts=0,cancelled=0;const response=deferred();
  const f=portalPageFixture(async path=>{
    if(path==='portal/workspace')return workspacePacket({accounts:[],grants:[],events:[],held_units:0});
    if(path==='portal/auth/start'){starts++;return {session:'synthetic-session',auth_url:'https://example.invalid',expires_in:1};}
    if(path==='portal/auth/poll'){polls++;return response.promise;}
    if(path==='portal/auth/cancel'){cancelled++;return {state:'cancelled'};}
    throw new Error('unexpected operation');
  },{Date:{now:()=>now}});
  f.render();f.h.effects();await flush();f.render();f.h.effects();
  const add=()=>descendants(f.render()).find(n=>n.type==='button'&&JSON.stringify(n.props.children).includes('扫码添加账号'));
  add().props.onClick();await flush();f.render();f.h.effects();
  const tick=[...f.timers.values()][0];const pending=tick();await flush();
  now=2000;await tick();assert.equal(polls,1);assert.equal(cancelled,0);assert.equal(starts,1);
  assert.equal(descendants(f.render()).some(n=>n.props?.role==='dialog'),true);
  response.resolve({state:'complete'});await pending;f.render();f.h.effects();
  assert.equal(descendants(f.render()).some(n=>n.props?.role==='dialog'),false);
  assert.doesNotMatch(JSON.stringify(f.render()),/重新添加/);
});

test('QR expiry after uncertain polling requires a read before another mutation', async () => {
  let now=0,polls=0,starts=0;
  const f=portalPageFixture(async path=>{
    if(path==='portal/workspace')return workspacePacket({accounts:[],grants:[],events:[],held_units:0});
    if(path==='portal/auth/start'){starts++;return {session:'synthetic-session',auth_url:'https://example.invalid',expires_in:1};}
    if(path==='portal/auth/poll'){polls++;throw new Error('synthetic offline');}
    if(path==='portal/auth/cancel')return {state:'cancelled'};
    throw new Error('unexpected operation');
  },{Date:{now:()=>now}});
  f.render();f.h.effects();await flush();f.render();f.h.effects();
  const add=()=>descendants(f.render()).find(n=>n.type==='button'&&JSON.stringify(n.props.children).includes('扫码添加账号'));
  add().props.onClick();await flush();f.render();f.h.effects();
  const tick=[...f.timers.values()][0];await tick();now=2000;await tick();
  assert.match(JSON.stringify(f.render()),/结果待核对/);
  assert.equal(add().props.disabled,true);assert.equal(polls,1);assert.equal(starts,1);
  assert.equal(descendants(f.render()).some(n=>n.type==='button'&&n.props.children==='重试当前二维码'),false);
  descendants(f.render()).find(n=>n.type==='button'&&n.props.children==='关闭').props.onClick();await flush();
  assert.equal(add().props.disabled,true);assert.equal(starts,1);
});

test('key page distinguishes enabled key, model discovery and paid dispatch without exposing other users', async () => {
  const f = portalKeyFixture(async () => ({items: [{id: 7, label: 'synthetic-owned', prefix: 'synthetic', enabled: true, created_at: 1}],
    allowed_models: ['deepseek-fixture'], model_discovery_enabled: true, pool_enabled: false, dispatch_enabled: false,
    dispatch_note: 'synthetic dispatch reason'}));
  f.render(); f.h.effects(); await flush();
  const view = JSON.stringify(f.render());
  assert.match(view, /模型调用暂未开放/);
  assert.match(view, /可读取模型目录/);
  assert.match(view, /synthetic dispatch reason/);
  assert.match(view, /已启用/);
});

test('approved key page states wallet cap and does not show the unpaid-activation warning', async () => {
  const f = portalKeyFixture(async () => ({items: [], allowed_models: ['deepseek-fixture'],
    model_discovery_enabled: true, pool_enabled: true, dispatch_enabled: true, dispatch_note: 'approved'}));
  f.render(); f.h.effects(); await flush();
  const view = JSON.stringify(f.render());
  assert.doesNotMatch(view, /模型调用暂未开放/);
  assert.match(view, /余额为零拒绝新请求/);
  assert.match(view, /超出部分由平台承担/);
  assert.match(view, /不扣未来签到/);
});

function authRaceFixture() {
  const harness = hookHarness();
  const cache = new Map();
  const reads = [];
  const window = {sessionStorage: {
    getItem: key => cache.get(key) ?? null,
    setItem: (key, value) => cache.set(key, value),
    removeItem: key => cache.delete(key),
  }, location: {href: '/login'}};
  const authApi = {
    me: signal => {const read = {...deferred(), signal}; reads.push(read); return read.promise;},
    login: async username => ({ok: true, username, role: 'viewer'}),
    logout: async () => ({ok: true}),
  };
  const {AuthProvider} = load('lib/auth-context.tsx', {
    react: harness.react, 'react/jsx-runtime': {jsx: (type, props) => ({type, props})},
    '@/lib/use-hydrated': {useHydrated: () => true},
    '@/lib/api': {authApi}, '@/lib/base-path': {BASE_PATH: ''}, globals: {window, AbortController},
  });
  return {harness, cache, reads, window, authApi, render: () => harness.render(() => AuthProvider({children: null})).props.value};
}

for (const response of ['old identity', 'old unauthorized']) {
  test(`a delayed identity read cannot replace a successful login with ${response}`, async () => {
    const fixture = authRaceFixture();
    fixture.render(); fixture.harness.effects(); await flush();
    await fixture.render().login('new-user', 'fixture-password');
    assert.equal(fixture.render().me.username, 'new-user');
    if (response === 'old identity') fixture.reads[0].resolve({username: 'old-admin', role: 'admin'});
    else fixture.reads[0].reject({response: {status: 401}});
    await flush();
    assert.equal(fixture.render().me.username, 'new-user');
    assert.equal(fixture.render().isAdmin, false);
    assert.equal(JSON.parse(fixture.cache.get('wb-me')).username, 'new-user');
  });
}

test('confirmed logout fences an older identity read and cancels the request', async () => {
  const fixture = authRaceFixture();
  fixture.render(); fixture.harness.effects(); await flush();
  await fixture.render().logout();
  fixture.reads[0].resolve({username: 'old-admin', role: 'admin'}); await flush();
  assert.equal(fixture.render().me, null);
  assert.equal(fixture.cache.has('wb-me'), false);
  assert.equal(fixture.reads[0].signal.aborted, true);
});

test('identity refresh coalesces reads and Strict Mode replay replaces an abandoned read', async () => {
  const fixture = authRaceFixture();
  fixture.render(); fixture.harness.effects(); await flush();
  const joined = fixture.render().refresh(); await flush();
  assert.equal(fixture.reads.length, 1);
  fixture.harness.replayEffects(); await flush();
  assert.equal(fixture.reads[0].signal.aborted, true);
  assert.equal(fixture.reads.length, 2);
  fixture.reads[1].resolve({username: 'current', role: 'viewer'}); await flush();
  fixture.reads[0].resolve({username: 'obsolete', role: 'admin'}); await joined;
  assert.equal(fixture.render().me.username, 'current');
  fixture.harness.unmount();
});

function taskRunFixture() {
  const harness = hookHarness(), reads = [], starts = [], scheduled = [];
  let finished = 0;
  const start = deferred();
  const {useTaskRun} = load('lib/use-task-run.ts', {
    react: harness.react,
    '@/lib/api': {errText: String, accountApi: {
      taskRunStatus() { const read = deferred(); reads.push(read); return read.promise; },
      taskRunStart(...args) { starts.push(args); return start.promise; },
      taskRunStop: async () => ({ok: true, message: 'stopped'}),
    }},
    '@/lib/i18n/provider': {useI18n: () => ({t: key => key, tp: key => key})},
    '@/lib/toast': {notify: {ok() {}, err() {}, info() {}}},
    globals: {
      setTimeout(callback, delay) { const entry = {callback, delay}; scheduled.push(entry); return entry; },
      clearTimeout(entry) { entry.cancelled = true; },
    },
  });
  return {harness, reads, starts, start, scheduled, finished: () => finished,
    render: () => harness.render(() => useTaskRun({target: 'ALL', onFinished: () => {finished++;}}))};
}

test('task status read failures preserve running cadence and never announce false completion', async () => {
  const fixture = taskRunFixture();
  fixture.render(); fixture.harness.effects();
  fixture.reads[0].resolve({running: true}); await flush();
  assert.equal(fixture.scheduled[0].delay, 2000);
  fixture.scheduled[0].callback();
  fixture.reads[1].reject(new Error('temporary network outage')); await flush();
  assert.equal(fixture.finished(), 0);
  assert.equal(fixture.scheduled[1].delay, 2000);
  fixture.scheduled[1].callback();
  fixture.reads[2].resolve({running: false}); await flush();
  assert.equal(fixture.finished(), 1);
  assert.equal(fixture.scheduled[2].delay, 15000);
  fixture.harness.unmount();
});

test('task status responses arriving after unmount cannot write state', async () => {
  const fixture = taskRunFixture();
  fixture.render(); fixture.harness.effects(); fixture.harness.unmount();
  fixture.reads[0].resolve({running: true}); await flush();
  assert.equal(fixture.render().status, null);
  assert.equal(fixture.scheduled.length, 0);
});

test('task actions coalesce same-tick clicks before React rerenders', async () => {
  const fixture = taskRunFixture();
  fixture.render(); fixture.harness.effects();
  fixture.reads[0].resolve({running: false}); await flush();
  const actions = fixture.render();
  const first = actions.run('preview');
  const second = actions.run('preview');
  assert.equal(fixture.starts.length, 1);
  fixture.start.resolve({ok: true}); await flush();
  fixture.reads[1].resolve({running: true});
  await Promise.all([first, second]);
  fixture.harness.unmount();
});

function playgroundRaceFixture() {
  const harness = hookHarness(), streams = [];
  const jsx = (type, props) => ({type, props});
  const models = [{id: 'synthetic-model', efforts: []}];
  let realm = 'international';
  const {default: PlaygroundPage} = load('app/(main)/playground/page.tsx', {
    react: harness.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': new Proxy({}, {get: (_target, key) => key}),
    '@/components/common/layout/PageHeader': {PageHeader: 'header'},
    '@/components/common/layout/PageSectionTabs': {PageSectionTabs: 'tabs'},
    '@/components/common/states/LoadError': {LoadError: 'load-error'},
    '@/components/ui/button': {Button: 'button'}, '@/components/ui/badge': {Badge: 'badge'},
    '@/components/ui/skeleton': {Skeleton: 'skeleton'},
    '@/components/ui/ai-chat-input': {AiChatInput: 'chat-input'},
    '@/components/ui/copy-button': {CopyButton: 'copy-button'},
    '@/lib/api': {playgroundApi: {}, errText: String, channelRejectionText: String},
    '@/lib/chat-stream': {streamChat(options) { const stream = {...deferred(), options}; streams.push(stream); return stream.promise; }},
    '@/lib/use-async-data': {useAsyncAll: () => ({values: {models}, errors: {}, reload() {}})},
    '@/lib/base-path': {withBasePath: value => value}, '@/lib/toast': {notify: {info() {}}},
    '@/lib/i18n/provider': {useT: () => (key, params) => params ? `${key}:${JSON.stringify(params)}` : key},
    '@/lib/format': {fmtCredit: String}, '@/lib/utils': {cn: (...values) => values.join(' ')},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: true})},
    '@/lib/realm-context': {useRealm: () => ({realm, label: realm})},
    '@/lib/platform-context': {usePlatform: () => ({platform: 'workbuddy'})},
    globals: {AbortController},
  });
  const render = () => harness.render(PlaygroundPage);
  const input = () => reviewNodes(render()).find(node => node.type === 'chat-input').props;
  return {harness, streams, render, input, setRealm(value) {realm = value;}};
}

test('switching playground realm invalidates old stream callbacks, usage, and finalizers', async () => {
  const fixture = playgroundRaceFixture();
  fixture.render(); fixture.harness.effects();
  fixture.input().onChange('old question');
  const oldSend = fixture.input().onSend();
  assert.equal(fixture.streams.length, 1);
  fixture.setRealm('china'); fixture.render(); fixture.harness.effects();
  assert.equal(fixture.streams[0].options.signal.aborted, true);
  assert.equal(fixture.input().streaming, false);
  fixture.input().onChange('new question');
  const newSend = fixture.input().onSend();
  assert.equal(fixture.streams.length, 2);
  fixture.streams[0].options.onDelta('obsolete answer', 'obsolete answer');
  fixture.streams[0].resolve({answer: 'obsolete answer', usage: {credit: 99}, aborted: true});
  await oldSend;
  assert.equal(fixture.input().streaming, true);
  assert.doesNotMatch(JSON.stringify(fixture.render()), /obsolete answer|\\"v\\":\\"99\\"/);
  fixture.streams[1].resolve({answer: 'current answer', usage: {credit: 2}, sawDone: true});
  await newSend;
  assert.equal(fixture.input().streaming, false);
  assert.match(JSON.stringify(fixture.render()), /current answer/);
  assert.match(JSON.stringify(fixture.render()), /\\"v\\":\\"2\\"/);
  fixture.harness.unmount();
});

function accountCreditFixture() {
  const harness = hookHarness(), snapshots = [], refreshes = [], actions = [], notices = [];
  const jsx = (type, props) => ({type, props});
  const t = key => key;
  const refresh = async () => true;
  const groups = [{id: null, name: 'Default', is_default: true, enabled: true, base_url: 'http://a.invalid'},
    {id: 1, name: 'Pool B', is_default: false, enabled: true, base_url: 'http://b.invalid'}];
  const mocks = {
    react: harness.react, 'react/jsx-runtime': {jsx, jsxs: jsx}, 'next/link': {default: 'Link'},
    'lucide-react': new Proxy({}, {get: (_target, key) => key}),
    '@/lib/api': {errText: String, upstreamApi: {}, upstreamsApi: {}, accountApi: {
      proxies: async () => ({routes: []}),
      cachedCredits(groupId) { const read = {...deferred(), groupId}; snapshots.push(read); return read.promise; },
      refreshCredits(force, groupId, staleOnly) { const read = {...deferred(), force, groupId, staleOnly}; refreshes.push(read); return read.promise; },
      checkin(file, groupId) { const action = {...deferred(), file, groupId}; actions.push(action); return action.promise; },
    }},
    '@/lib/use-heartbeat': {useHeartbeat() {}},
    '@/lib/use-async-data': {useAsyncAll: (fetchers, deps) => ({errors: {}, reload: refresh, refresh,
      values: 'pool' in fetchers ? {pool: {accounts: [{uid: 'shared-uid', file: 'workbuddy-shared.json',
        nickname: deps[0] === null ? 'Default account' : 'Pool B account', realm: 'cn',
        expires_at: 0, credits: 10}], manageable: true, upstream: {name: 'synthetic'}}}
        : 'groups' in fetchers ? {groups: {items: groups}} : {upstream: null}})},
    '@/lib/account-status': {AVAILABILITY_GROUPS: [], availabilityOf: () => 'healthy',
      availabilityGroup: () => 'healthy', availabilityGroupLabelKey: String,
      availabilityLabelKey: String, isDegraded: () => false, mergePoolStatus: value => value,
      rateLimitedModels: () => []},
    '@/lib/account-list': {groupCounts: rows => ({all: rows.length}), matchesQuery: () => true,
      selectAccounts: rows => rows,
      paginate: (rows, page) => ({rows, page, pages: 1, total: rows.length})},
    '@/lib/format': {fmtNumber: value => `credit:${value}`, fmtDateTime: String, fmtAgo: String,
      fmtRemain: String, expiryBarPercent: () => 0, expiryVisual: () => ({})},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: true, canAddAccounts: true, loading: false, me: {role: 'admin'}})},
    '@/lib/realm-context': {useRealm: () => ({realm: 'cn', label: 'cn'}), realmLabel: String},
    '@/lib/platform-context': {usePlatform: () => ({platform: 'workbuddy', label: 'WorkBuddy'})},
    '@/lib/i18n/provider': {useT: () => t},
    '@/lib/toast': {notify: Object.fromEntries(['ok', 'warn', 'err', 'info'].map(name =>
      [name, (...args) => notices.push([name, ...args])]))},
    globals: {window: {dispatchEvent() {}}, Event},
  };
  const source = fs.readFileSync(path.join(root, 'app/(main)/accounts/page.tsx'), 'utf8');
  for (const [, name] of source.matchAll(/from '(@\/components\/[^']+)'/g)) {
    mocks[name] = new Proxy({}, {get: (_target, key) => key});
  }
  const {default: AccountsPage} = load('app/(main)/accounts/page.tsx', mocks);
  const render = () => harness.render(() => AccountsPage().type());
  const nodes = () => reviewNodes(render()).flatMap(node => node.type === 'PageHeader'
    ? [node, ...reviewNodes(node.props.actions)] : [node]);
  const button = title => nodes().find(node => node.type === 'Button' && node.props.title === title).props;
  const credits = () => nodes().filter(node => node.props?.title === 'accounts.creditsTitle').map(node => node.props.children);
  const selectB = () => {
    nodes().find(node => node.type === 'Button' && JSON.stringify(node.props.children)?.includes('Pool B')).props.onClick();
    render(); harness.effects();
  };
  const response = value => ({credits: {'shared-uid': value},
    meta: {'shared-uid': {cached: false, source: 'cache', expiries: []}}, failed: [], total: 1, succeeded: 1});
  const settleAutomatic = async value => {
    snapshots.at(-1).resolve(response(value)); await flush();
    const read = refreshes.at(-1); assert.equal(read.force, false);
    read.resolve(response(value)); await flush();
  };
  return {harness, snapshots, refreshes, actions, notices, render, button, credits, selectB, response, settleAutomatic};
}

test('old group manual credit responses cannot replace the selected group balances', async () => {
  const fixture = accountCreditFixture();
  fixture.render(); fixture.harness.effects(); await fixture.settleAutomatic(20);
  const pending = fixture.button('accounts.refreshCreditsTitle').onClick();
  const oldRead = fixture.refreshes.at(-1);
  fixture.selectB(); await fixture.settleAutomatic(31);
  oldRead.resolve(fixture.response(999)); await pending;
  assert.deepEqual(fixture.credits(), ['credit:31', 'credit:31']);
  fixture.harness.unmount();
});

test('an older automatic credit read cannot overwrite a newer manual refresh', async () => {
  const fixture = accountCreditFixture();
  fixture.render(); fixture.harness.effects();
  fixture.snapshots[0].resolve(fixture.response(10)); await flush();
  const automatic = fixture.refreshes[0];
  const pending = fixture.button('accounts.refreshCreditsTitle').onClick();
  fixture.refreshes[1].resolve(fixture.response(200)); await pending;
  automatic.resolve(fixture.response(11)); await flush();
  assert.deepEqual(fixture.credits(), ['credit:200', 'credit:200']);
  fixture.harness.unmount();
});

test('old group single-account action responses cannot change the new group credits', async () => {
  const fixture = accountCreditFixture();
  fixture.render(); fixture.harness.effects(); await fixture.settleAutomatic(20);
  const pending = fixture.button('accounts.checkin').onClick();
  fixture.selectB(); await fixture.settleAutomatic(31);
  fixture.actions[0].resolve({ok: true, credits: 999, expiries: []}); await pending;
  assert.deepEqual(fixture.credits(), ['credit:31', 'credit:31']);
  fixture.harness.unmount();
});

test('manual credit refresh cannot write or notify after the account page unmounts', async () => {
  const fixture = accountCreditFixture();
  fixture.render(); fixture.harness.effects(); await fixture.settleAutomatic(20);
  const pending = fixture.button('accounts.refreshCreditsTitle').onClick();
  fixture.harness.unmount();
  fixture.refreshes.at(-1).resolve(fixture.response(999)); await pending;
  assert.deepEqual(fixture.credits(), ['credit:20', 'credit:20']);
  assert.equal(fixture.notices.length, 0);
});

test('manual credit refresh prevents same-tick duplicate provider fanout', async () => {
  const fixture = accountCreditFixture();
  fixture.render(); fixture.harness.effects(); await fixture.settleAutomatic(20);
  const click = fixture.button('accounts.refreshCreditsTitle').onClick;
  const first = click(), second = click();
  assert.equal(fixture.refreshes.filter(read => read.force).length, 1);
  fixture.refreshes.at(-1).resolve(fixture.response(25)); await Promise.all([first, second]);
  fixture.harness.unmount();
});

function clipboardFixture() {
  const harness = hookHarness(), pending = deferred(), timers = new Map(), notices = [], copies = [];
  const jsx = (type, props, key) => ({type, props, key});
  const {CopyButton} = load('components/ui/copy-button.tsx', {
    react: harness.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': {Check:'icon', Copy:'icon', Loader2:'icon', Share2:'icon'},
    '@/components/ui/button': {Button:'button'}, '@/lib/utils': {cn: (...parts) => parts.join(' ')},
    '@/lib/i18n/provider': {useT: () => key => key},
    '@/lib/toast': {notify: {ok: value => notices.push(value), err: value => notices.push(value)}},
    '@/lib/format': {copyText: value => { copies.push(value); return pending.promise; }},
    globals: {window: {setTimeout: fn => { const id=timers.size+1;timers.set(id,fn);return id; },
      clearTimeout: id => timers.delete(id)}},
  });
  const wrapper = CopyButton({value:'synthetic-value'});
  const render = () => harness.render(() => wrapper.type(wrapper.props));
  return {harness, pending, timers, notices, copies, render, wrapper, CopyButton};
}

test('clipboard double click performs one write and its feedback timer is owned by the mounted value', async () => {
  const fixture=clipboardFixture();
  const tree=fixture.render();fixture.harness.effects();
  const first=tree.props.onClick(),second=tree.props.onClick();
  assert.deepEqual(fixture.copies,['synthetic-value']);
  fixture.pending.resolve(true);await Promise.all([first,second]);
  assert.equal(fixture.timers.size,1);
  fixture.harness.unmount();
  assert.equal(fixture.timers.size,0);
  assert.notEqual(fixture.wrapper.key,fixture.CopyButton({value:'replacement'}).key);
});

test('clipboard completion after unmount cannot show old-value feedback or schedule a new timer', async () => {
  const fixture=clipboardFixture();
  const tree=fixture.render();fixture.harness.effects();
  const pending=tree.props.onClick();fixture.harness.unmount();
  fixture.pending.resolve(true);await pending;
  assert.equal(fixture.notices.length,0);
  assert.equal(fixture.timers.size,0);
});

test('IME Enter in the actual account, whitelist and chat handlers does not submit', () => {
  for (const relative of ['components/common/accounts/AccountNoteDialog.tsx',
    'components/common/accounts/AccountWeightDialog.tsx','components/common/keys/ModelWhitelistInput.tsx',
    'components/ui/ai-chat-input.tsx']) {
    const source=ts.createSourceFile(relative,fs.readFileSync(path.join(root,relative),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
    let handler;
    const visit = node => {
      if (ts.isJsxAttribute(node) && node.name.getText(source)==='onKeyDown' && node.initializer?.expression) handler ??= node.initializer.expression.getText(source);
      ts.forEachChild(node,visit);
    };
    visit(source);assert.ok(handler,relative);
    let submitted=0;
    const context={module:{exports:null},save:()=>submitted++,submit:()=>submitted++,add:()=>submitted++,
      draft:'synthetic',names:[],remove:()=>{},};
    vm.runInNewContext(ts.transpileModule(`module.exports=(${handler})`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
    const event={key:'Enter',shiftKey:false,preventDefault(){},nativeEvent:{isComposing:true,keyCode:229}};
    context.module.exports(event);assert.equal(submitted,0,relative);
    event.nativeEvent={isComposing:false,keyCode:13};
    context.module.exports(event);assert.equal(submitted,1,relative);
  }
});

test('placeholder rotation honors reduced motion and cleans its only timer', () => {
  const relative='components/ui/ai-chat-input.tsx';
  const source=ts.createSourceFile(relative,fs.readFileSync(path.join(root,relative),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  let effect;
  const visit=node=>{ if(ts.isCallExpression(node)&&node.expression.getText(source)==='useEffect') effect??=node.arguments[0].getText(source);ts.forEachChild(node,visit); };
  visit(source);assert.ok(effect);
  for(const reducedMotion of [false,true]) {
    const timers=new Set();
    const context={module:{exports:null},expanded:false,reducedMotion,hints:['a','b'],setPhIndex:()=>{},
      setInterval:()=>{timers.add(1);return 1;},clearInterval:id=>timers.delete(id)};
    vm.runInNewContext(ts.transpileModule(`module.exports=(${effect})`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
    const cleanup=context.module.exports();
    assert.equal(timers.size,reducedMotion?0:1);
    cleanup?.();assert.equal(timers.size,0);
  }
});
