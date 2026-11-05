const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};

// Execute the shipped TSX and its real event handlers. Only React scheduling,
// visual primitives, and the remote API are substituted; no business logic is
// copied into the fixture.
function hooks() {
  const slots = [];
  let cursor = 0, queued = [], unmounted = false, lateWrites = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const react = {
    useState(initial) {
      const index = cursor++;
      slots[index] ??= {value: typeof initial === 'function' ? initial() : initial};
      return [slots[index].value, next => {
        if (unmounted) lateWrites++;
        slots[index].value = typeof next === 'function' ? next(slots[index].value) : next;
      }];
    },
    useRef(initial) { const index = cursor++; slots[index] ??= {current: initial}; return slots[index]; },
    useCallback(fn, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = {value: fn, deps};
      return slots[index].value;
    },
    useMemo(fn, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = {value: fn(), deps};
      return slots[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) {
        queued.push(() => { slots[index]?.cleanup?.(); slots[index] = {deps, effect, cleanup: effect()}; });
      }
    },
  };
  return {
    react,
    render(fn) { cursor = 0; return fn(); },
    effects() { const work = queued; queued = []; work.forEach(fn => fn()); },
    replayEffects() {
      slots.forEach(slot => slot?.cleanup?.());
      slots.forEach(slot => { if (slot?.effect) slot.cleanup = slot.effect(); });
    },
    unmount() { unmounted = true; slots.forEach(slot => slot?.cleanup?.()); },
    lateWrites: () => lateWrites,
  };
}

const jsx = (type, props) => ({type, props});
const primitives = new Proxy({}, {get: (_target, key) => key});
const t = key => key;

function load(relative, mocks) {
  const source = fs.readFileSync(path.join(root, relative), 'utf8');
  const code = ts.transpileModule(source, {fileName: relative, compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const compiled = {exports: {}};
  vm.runInNewContext(code, {
    module: compiled, exports: compiled.exports, Promise, Error, console,
    require(name) {
      if (name in mocks) return mocks[name];
      if (name.startsWith('@/components/') || name === 'lucide-react') return primitives;
      throw new Error(`Unexpected dependency ${name}`);
    },
    ...mocks.globals,
  }, {filename: relative});
  return compiled.exports;
}

function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== 'object' || !tree.props) return [];
  return [tree, ...Object.values(tree.props).flatMap(nodes)];
}

function hasLabel(node, label) {
  return node.type === 'Button' && [node.props.children].flat().includes(label);
}

const tokenPlan = {enabled: true, interval_days: 9, hour: 17, last_run: 101, next_run: 202};
const dailyPlan = Object.fromEntries(['checkin', 'compensation', 'claim', 'full'].map((key, i) =>
  [key, {enabled: i !== 3, hour: 8 + i, last_run: '2025-01-01-08'}]));

function planFixture(kind, admin = true) {
  const h = hooks(), reads = [], saves = [], runs = [], notices = [];
  let ran = 0;
  const record = (entries, args) => { const entry = {...deferred(), args}; entries.push(entry); return entry.promise; };
  const {RefreshPlanCard} = load('components/common/accounts/RefreshPlanCard.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    '@/lib/i18n/provider': {useT: () => t},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: admin})},
    '@/lib/format': {fmtDateTime: n => `date:${n}`},
    '@/lib/toast': {notify: Object.fromEntries(['ok', 'warn', 'err'].map(level =>
      [level, (...args) => notices.push({level, args})]))},
    '@/lib/api': {errText: String, accountApi: {
      refreshPlan: () => record(reads, []), dailyPlan: () => record(reads, []),
      saveRefreshPlan: (...args) => record(saves, args), saveDailyPlan: (...args) => record(saves, args),
      runRefreshPlan: (...args) => record(runs, args), runDailyTask: (...args) => record(runs, args),
    }},
  });
  const card = RefreshPlanCard({onRan: () => { ran++; }});
  const section = nodes(card).find(node => node.type.name === (kind === 'token' ? 'TokenPlanSection' : 'DailyTasksSection'));
  const render = () => h.render(() => section.type(section.props));
  const plan = kind === 'token' ? tokenPlan : dailyPlan;
  return {
    h, reads, saves, runs, notices, render, plan, ran: () => ran,
    button: label => nodes(render()).find(node => hasLabel(node, label) && node.props.onClick),
    error: () => nodes(render()).find(node => node.type === 'LoadError'),
    ready: async () => { render(); h.effects(); reads[0].resolve(plan); await flush(); render(); },
    result: kind === 'token' ? {renewed: 1, checked: 1, failed: 0} : {ok: true, message: 'done'},
  };
}

for (const kind of ['token', 'daily']) {
  test(`${kind} plan load failure is persistent and retryable, without an invented configuration`, async () => {
    const f = planFixture(kind, false);
    f.render(); f.h.effects();
    f.reads[0].reject(new Error('synthetic offline')); await flush();
    const tree = f.render();
    assert.equal(tree.type, 'LoadError');
    assert.equal(typeof tree.props.onRetry, 'function');
    assert.doesNotMatch(JSON.stringify(tree), /common\.loading|tasks\.planNever|tasks\.planDisabled/);
    assert.equal(nodes(tree).some(node => node.type === 'Switch' || node.type === 'Button'), false);
    tree.props.onRetry();
    assert.equal(f.reads.length, 2, 'a read-only user can retry the failed read');
    f.reads[1].resolve(f.plan); await flush();
    assert.equal(f.error(), undefined);
    assert.ok(nodes(f.render()).filter(node => node.type === 'Switch').every(node => node.props.disabled));
    await f.button('tasks.planRunNow').props.onClick();
    await f.button('common.save').props.onClick();
    assert.equal(f.runs.length, 0); assert.equal(f.saves.length, 0);
    f.h.unmount();
  });

  test(`${kind} plan rejects a superseded read and a late unmounted read`, async () => {
    const f = planFixture(kind);
    f.render(); f.h.effects(); f.h.replayEffects();
    assert.equal(f.reads.length, 2);
    f.reads[1].resolve(f.plan); await flush();
    const before = JSON.stringify(f.render());
    f.reads[0].reject(new Error('old request failed')); await flush();
    assert.equal(JSON.stringify(f.render()), before);
    assert.equal(f.notices.length, 0);
    f.h.unmount();

    const pending = planFixture(kind);
    pending.render(); pending.h.effects(); pending.h.unmount();
    pending.reads[0].resolve(pending.plan); await flush();
    assert.equal(pending.h.lateWrites(), 0);
    assert.equal(pending.notices.length, 0);
  });

  test(`${kind} plan preserves confirmed data but blocks actions after a failed post-run refresh`, async () => {
    const f = planFixture(kind); await f.ready();
    const run = f.button('tasks.planRunNow').props.onClick;
    const first = run(), duplicate = run();
    assert.equal(f.runs.length, 1);
    f.runs[0].resolve(f.result); await flush();
    f.reads[1].reject(new Error('refresh unavailable'));
    await Promise.all([first, duplicate]);
    assert.ok(f.error()); assert.equal(f.ran(), 1);
    const switches = nodes(f.render()).filter(node => node.type === 'Switch');
    assert.equal(switches[0].props.checked, true);
    assert.ok(switches.every(node => node.props.disabled));
    await f.button('tasks.planRunNow').props.onClick();
    await f.button('common.save').props.onClick();
    assert.equal(f.runs.length, 1); assert.equal(f.saves.length, 0);
    f.error().props.onRetry();
    f.reads[2].resolve(f.plan); await flush();
    assert.equal(f.error(), undefined);
    assert.equal(f.button('tasks.planRunNow').props.disabled, false);
    f.h.unmount();
  });

  test(`${kind} plan serializes saves and runs and ignores a write completing after unmount`, async () => {
    const f = planFixture(kind); await f.ready();
    nodes(f.render()).find(node => node.type === 'Switch').props.onCheckedChange(false);
    const save = f.button('common.save').props.onClick;
    const first = save(), duplicate = save();
    await f.button('tasks.planRunNow').props.onClick();
    assert.equal(f.saves.length, 1); assert.equal(f.runs.length, 0);
    assert.equal(f.button('common.save').props.disabled, true);
    f.h.unmount();
    f.saves[0].resolve(f.plan); await Promise.all([first, duplicate]);
    assert.equal(f.h.lateWrites(), 0);
    assert.equal(f.notices.length, 0);
  });
}

test('daily full task requires confirmation and its promise includes the post-run read', async () => {
  const f = planFixture('daily'); await f.ready();
  const dialog = nodes(f.render()).find(node => node.type === 'ConfirmDialog');
  assert.equal(dialog.props.title, 'tasks.runFullConfirmTitle');
  assert.equal(dialog.props.trigger.props.onClick, undefined);
  assert.equal(f.runs.length, 0);
  let settled = false;
  const work = dialog.props.onConfirm().then(() => { settled = true; });
  const duplicate = dialog.props.onConfirm();
  assert.equal(f.runs.length, 1);
  assert.deepEqual(f.runs[0].args, ['full']);
  assert.equal(nodes(f.render()).find(node => node.type === 'ConfirmDialog').props.confirmDisabled, true);
  f.runs[0].resolve(f.result); await flush();
  assert.equal(settled, false);
  f.reads[1].resolve(f.plan); await Promise.all([work, duplicate]);
  assert.equal(settled, true); assert.equal(f.ran(), 1);
  f.h.unmount();
});

const claimInfo = {
  title: 'Synthetic packet', quota_kind: 'credit', shares: 2, left: 1, models: [],
  expires_at: 200, expired: false, claimed: false, my_key: null, my_amount: null,
};
const drawResult = {key: 'synthetic-own-key', amount: 3.25, quota_kind: 'credit', models: [], expires_at: 200};

function claimFixture(copy = async () => {}) {
  const h = hooks(), reads = [], draws = [], notices = [], copies = [];
  const record = (entries, code) => { const entry = {...deferred(), code}; entries.push(entry); return entry.promise; };
  const {default: ClaimPage} = load('app/claim/page.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    '@/lib/i18n/provider': {useT: () => t}, '@/lib/base-path': {BASE_PATH: '/gateway'},
    '@/lib/format': {fmtDateTime: String, fmtNumber: String},
    '@/lib/toast': {notify: Object.fromEntries(['ok', 'err'].map(level =>
      [level, (...args) => notices.push({level, args})]))},
    '@/lib/api': {errText: String, claimApi: {
      info: code => record(reads, code), draw: code => record(draws, code),
    }},
    globals: {URLSearchParams, window: {location: {origin: 'https://gateway.test', search: '?code=synthetic-code'}},
      navigator: {clipboard: {writeText: text => { copies.push(text); return copy(text); }}}},
  });
  const render = () => h.render(ClaimPage);
  return {
    h, reads, draws, notices, copies, render,
    button: label => nodes(render()).find(node => hasLabel(node, label)),
    ready: async info => { render(); h.effects(); reads[0].resolve(info); await flush(); render(); },
    dialog: () => nodes(render()).find(node => node.type === 'Dialog'),
  };
}

for (const state of [{left: 0, expired: false}, {left: 1, expired: true}]) {
  test(`a claimant can recover their own key when left=${state.left} and expired=${state.expired}`, async () => {
    const f = claimFixture();
    await f.ready({...claimInfo, ...state, claimed: true, my_key: drawResult.key, my_amount: drawResult.amount});
    assert.match(JSON.stringify(f.render()), /synthetic-own-key/);
    assert.ok(f.button('claim.copy')); assert.equal(f.button('claim.open'), undefined);
    await f.button('claim.copy').props.onClick();
    assert.deepEqual(f.copies, [drawResult.key]); assert.equal(f.draws.length, 0);
    f.h.unmount();
  });
}

test('successful draw becomes recoverable immediately and closing does not reopen the draw action', async () => {
  const copy = deferred(), f = claimFixture(() => copy.promise);
  await f.ready(claimInfo);
  const draw = f.button('claim.open').props.onClick;
  const first = draw(), duplicate = draw();
  assert.equal(f.draws.length, 1);
  f.draws[0].resolve(drawResult); await Promise.all([first, duplicate]);
  assert.equal(f.dialog().props.open, true);
  assert.equal(f.button('claim.open'), undefined);
  const close = f.button('claim.done').props.onClick();
  assert.equal(f.dialog().props.open, false, 'clipboard permission must not keep the result dialog open');
  assert.match(JSON.stringify(f.render()), /synthetic-own-key/);
  assert.equal(f.button('claim.open'), undefined);
  await draw(); assert.equal(f.draws.length, 1, 'the old button callback cannot draw again before React commits');
  copy.resolve(); await close;
  assert.deepEqual(f.copies, [drawResult.key]);
  f.h.unmount();
});

test('failed draws can be retried and late reads or draws cannot update an unmounted claim page', async () => {
  const f = claimFixture(); await f.ready(claimInfo);
  const failed = f.button('claim.open').props.onClick();
  f.draws[0].reject(new Error('temporary outage')); await failed;
  assert.equal(f.button('claim.open').props.disabled, false);
  const retried = f.button('claim.open').props.onClick();
  assert.equal(f.draws.length, 2);
  f.h.unmount();
  f.draws[1].resolve(drawResult); await retried;
  assert.equal(f.h.lateWrites(), 0);
  assert.equal(f.notices.length, 1, 'only the live failure may notify');

  const read = claimFixture(); read.render(); read.h.effects(); read.h.unmount();
  read.reads[0].resolve({...claimInfo, claimed: true, my_key: drawResult.key}); await flush();
  assert.equal(read.h.lateWrites(), 0); assert.equal(read.notices.length, 0);
});

test('red packet share URL keeps the deployment prefix and encodes the code in displayed and copied links', async () => {
  const h = hooks(), copies = [];
  const packet = {code: 'synthetic + / ?', keys: [], quota_kind: 'credit'};
  const {default: RedPacketsPage} = load('app/(main)/red-packets/page.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    'next/navigation': {useRouter: () => ({replace() {}})},
    '@/lib/platform-context': {usePlatform: () => ({platform: 'workbuddy'})},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: true})},
    '@/lib/i18n/provider': {useT: () => t}, '@/lib/base-path': {BASE_PATH: '/gateway'},
    '@/lib/format': {fmtDateTime: String, fmtNumber: String},
    '@/lib/toast': {notify: {ok() {}, err() {}, warn() {}}},
    '@/lib/use-heartbeat': {useHeartbeat() {}},
    '@/lib/use-async-data': {useAsyncAll: () => ({values: {list: []}, reload: async () => true})},
    '@/lib/api': {errText: String, redPacketApi: {create: async () => packet}},
    globals: {window: {location: {origin: 'https://gateway.test'}},
      navigator: {clipboard: {writeText: async text => { copies.push(text); }}}},
  });
  const render = () => h.render(RedPacketsPage);
  const button = label => nodes(render()).find(node => hasLabel(node, label));
  render(); h.effects(); button('redPacket.new').props.onClick();
  await button('redPacket.create').props.onClick();
  const expected = 'https://gateway.test/gateway/claim/?code=synthetic%20%2B%20%2F%20%3F';
  assert.match(JSON.stringify(render()), /https:\/\/gateway\.test\/gateway\/claim\/\?code=synthetic%20%2B%20%2F%20%3F/);
  await button('redPacket.copyLink').props.onClick();
  assert.deepEqual(copies, [expected]);
  h.unmount();
});
