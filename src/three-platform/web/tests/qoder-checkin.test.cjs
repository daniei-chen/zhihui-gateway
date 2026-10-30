'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');

test('account pages do not mount or poll the internal reward history', () => {
  const source = fs.readFileSync(path.join(root, 'app/(main)/accounts/page.tsx'), 'utf8');
  assert.doesNotMatch(source, /QoderCheckinHistory|checkinLogs|rewardHistoryTitle/);
});

function render(state) {
  const source = fs.readFileSync(path.join(root, 'components/common/accounts/QoderCheckinHistory.tsx'), 'utf8');
  const code = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  const compiled = {exports: {}};
  const jsx = (type, props) => ({type, props});
  const fetches = [], listeners = new Map();
  const refresh = async () => {};
  const mocks = {
    react: {useEffect: fn => {fn();}}, 'react/jsx-runtime': {jsx, jsxs: jsx},
    '@/lib/api': {accountApi: {checkinLogs: (...args) => {fetches.push(args); return Promise.resolve({items: []});}}},
    '@/lib/format': {fmtDateTime: x => `date:${x}`, fmtNumber: String},
    '@/lib/use-async-data': {useAsyncAll: (fetchers, deps) => {
      assert.deepEqual(Array.from(deps), ['global']); void fetchers.history();
      return {values: {}, errors: {}, refresh, reload: refresh, ...state};
    }},
    '@/lib/use-heartbeat': {useHeartbeat: (fn, ms) => {assert.equal(fn, refresh); assert.equal(ms, 30000);}},
    '@/lib/realm-context': {useRealm: () => ({realm: 'global'})},
    '@/lib/i18n/provider': {useT: () => (key, params) => key + (params ? ':' + JSON.stringify(params) : '')},
    '@/lib/operational-evidence': {evidenceAccess: error => !error ? null : error.response?.status === 403 ? 'forbidden' : error.response?.status === 501 ? 'unavailable' : 'stale'},
    '@/components/common/states/LoadError': {LoadError: 'load-error'}, '@/components/ui/button': {Button: 'button'},
  };
  vm.runInNewContext(code, {module: compiled, exports: compiled.exports,
    window: {addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener() {}},
    require: name => {assert.ok(name in mocks, name); return mocks[name];}}, {filename: 'QoderCheckinHistory.tsx'});
  return {tree: compiled.exports.QoderCheckinHistory(), fetches, listeners};
}
function nodes(tree) {
  const found = [];
  function visit(node) {if (!node || typeof node !== 'object') return; if (Array.isArray(node)) {node.forEach(visit); return;} found.push(node); visit(node.props?.children);}
  visit(tree); return found;
}

test('reward history scopes reads to realm and never presents zero/unknown as green rewards', () => {
  const result = render({values: {history: {items: [
    {id: 1, ts: 1, nickname: 'rewarded', reward_status: 'rewarded', earned_credit: 100},
    {id: 2, ts: 2, nickname: 'zero', success: true, reward_status: 'no_reward', earned_credit: 0},
    {id: 3, ts: 3, nickname: 'unknown', success: true, reward_status: 'unknown'},
  ], scheduler: {state: 'uncertain'}}}});
  assert.deepEqual(Array.from(result.fetches[0]), [20, 0, undefined, 30, 'global']);
  const all = nodes(result.tree);
  assert.equal(all.filter(n => n.props?.className?.includes('text-emerald')).length, 1);
  const text = JSON.stringify(result.tree);
  assert.match(text, /rewardStatus_no_reward/); assert.match(text, /rewardStatus_unknown/);
  assert.match(text, /rewardSchedulerUncertain/); assert.match(text, /rewardZeroNote/);
  assert.ok(result.listeners.has('workbuddy-manager:accounts-changed'));
});

test('failed and loading history never masquerades as an empty successful query', () => {
  for (const state of [{errors: {history: Error('offline')}}, {isInitialLoading: true}]) {
    const result = render(state); const text = JSON.stringify(result.tree);
    assert.doesNotMatch(text, /rewardHistoryEmpty/);
    if (state.errors) assert.ok(nodes(result.tree).some(n => n.type === 'load-error'));
    else assert.match(text, /common.loading/);
  }
});

test('engine ownership and unverified takeover have distinct truthful notices', () => {
  for (const owner of ['engine', 'unverified', 'manager']) {
    const text = JSON.stringify(render({values: {history: {items: [], scheduler: {state: 'unknown', automation_owner: owner}}}}).tree);
    if (owner === 'engine') {assert.match(text, /rewardEngineScheduler/); assert.doesNotMatch(text, /rewardSchedulerOwnershipUnknown/);}
    else if (owner === 'unverified') {assert.match(text, /rewardSchedulerOwnershipUnknown/); assert.doesNotMatch(text, /rewardEngineScheduler/);}
    else assert.doesNotMatch(text, /rewardEngineScheduler|rewardSchedulerOwnershipUnknown/);
    assert.doesNotMatch(text, /rewardGranted/);
  }
});

test('reward history hides retained evidence after permission loss and marks network staleness', () => {
  const values = {history: {items: [{id: 1, nickname: 'retained-private-evidence', ts: 1, reward_status: 'rewarded', earned_credit: 100}]}};
  for (const status of [403, 501, 503]) {
    const text = JSON.stringify(render({values, errors: {history: {response: {status}}}}).tree);
    if (status === 503) {assert.match(text, /retained-private-evidence/); assert.match(text, /operations.stale/);}
    else assert.doesNotMatch(text, /retained-private-evidence/);
  }
});

test('reward evidence labels are defined in every supported locale', () => {
  const keys = ['rewardHistoryTitle', 'rewardEvidenceNote', 'rewardGranted', 'rewardStatus_already', 'rewardStatus_no_reward', 'rewardStatus_completed', 'rewardStatus_skipped', 'rewardStatus_pending', 'rewardStatus_failed', 'rewardStatus_unknown', 'rewardZeroNote', 'rewardHistoryEmpty', 'rewardHistoryWindow', 'rewardSchedulerUncertain', 'rewardEngineScheduler', 'rewardSchedulerOwnershipUnknown'];
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const tasks = JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json'))).tasks;
    for (const key of keys) assert.ok(typeof tasks[key] === 'string' && tasks[key].trim(), locale + ':' + key);
    assert.match(tasks.rewardGranted, /\{amount\}/);
    assert.match(tasks.rewardHistoryWindow, /\{shown\}/);
  }
});

test('the WorkBuddy-only tasks tab is not offered in Qoder or Trae', () => {
  const code = ts.transpileModule(fs.readFileSync(path.join(root, 'components/common/layout/PageSectionTabs.tsx'), 'utf8'),
    {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  for (const platform of ['workbuddy', 'qoder', 'trae']) {
    const compiled = {exports: {}}, jsx = (type, props) => ({type, props});
    const mocks = {
      'lucide-react': new Proxy({}, {get: (_, name) => name}), 'react/jsx-runtime': {jsx, jsxs: jsx},
      'next/navigation': {usePathname: () => '/accounts/'},
      '@/components/common/layout/SectionTabs': {SectionTabs: 'tabs'}, '@/lib/base-path': {BASE_PATH: ''},
      '@/lib/i18n/provider': {useT: () => key => key}, '@/lib/platform-context': {usePlatform: () => ({platform})},
      '@/lib/section-nav': {SECTION_NAV: {accounts: [{href: '/accounts', labelKey: 'accounts', iconKey: 'accounts'}, {href: '/tasks', labelKey: 'tasks', iconKey: 'tasks'}]},
        sectionNavFromPath: () => ({section: 'accounts', item: {href: '/accounts'}})},
    };
    vm.runInNewContext(code, {module: compiled, exports: compiled.exports, require: name => {assert.ok(name in mocks, name); return mocks[name];}});
    const tree = compiled.exports.PageSectionTabs();
    if (platform === 'workbuddy') assert.equal(tree.props.items.length, 2);
    else assert.equal(tree, null);
  }
});

test('credit tiles keep unknown and known zero neutral, without fabricated totals', () => {
  const code = ts.transpileModule(fs.readFileSync(path.join(root, 'components/dashboard/CreditFlowTile.tsx'), 'utf8'),
    {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  const compiled = {exports: {}}, jsx = (type, props) => ({type, props});
  vm.runInNewContext(code, {module: compiled, exports: compiled.exports, require: name => {
    if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    assert.equal(name, '@/lib/format'); return {fmtNumber: String};
  }});
  for (const value of [null, NaN, Infinity, 0, 100, -5]) {
    const tree = compiled.exports.CreditFlowTile({label: 'income', value, tone: 'net', title: 'evidence'});
    const all = nodes(tree), text = JSON.stringify(tree);
    if (value === null || !Number.isFinite(value)) {
      assert.match(text, /—/); assert.doesNotMatch(text, /text-emerald|text-red/);
    } else if (value === 0) assert.doesNotMatch(text, /text-emerald|text-red/);
    else assert.ok(all.some(node => node.props?.className?.includes(value > 0 ? 'text-emerald' : 'text-red')));
  }
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const copy = JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json'))).dashboard;
    assert.ok(copy.creditsIncomeUnavailable && copy.creditsConsumptionOnly && copy.creditsConsumptionUnknown);
    assert.ok(JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json'))).stats.retainedKeyLogsHint);
  }
});

test('actual usage table renders provider totals and unknown splits without inventing zero', () => {
  const source = fs.readFileSync(path.join(root, 'app/(main)/stats/page.tsx'), 'utf8');
  const start = source.indexOf('function BreakdownPanel('), end = source.indexOf('function StatsSkeleton()');
  assert.ok(start > 0 && end > start);
  const jsx = (type, props) => ({type, props});
  const compiled = {exports: {}};
  const metrics = {exports: {}};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(root, 'lib/usage-metrics.ts'), 'utf8'),
    {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, {module: metrics, exports: metrics.exports});
  const context = {module: compiled, exports: compiled.exports, useT: () => key => key,
    CHART_COLORS: ['blue'], ...metrics.exports, fmtCompact: value => `tokens:${value}`, fmtNumber: String,
    fmtCredit: value => value === null || value === undefined ? '—' : String(value),
    EmptyState: 'empty', Activity: 'activity', Table: 'table', TableHeader: 'thead', TableRow: 'tr',
    TableHead: 'th', TableBody: 'tbody', TableCell: 'td',
    require: name => {assert.equal(name, 'react/jsx-runtime'); return {jsx, jsxs: jsx};}};
  const code = ts.transpileModule(source.slice(start, end) + '\nexport {BreakdownPanel};',
    {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(code, context);
  const items = [{name: 'provider-total', key_id: 32, credit: 1, requests: 1, total_tokens: 190, prompt_tokens: null, completion_tokens: null},
                 {name: 'unknown', key_id: 40, credit: null, requests: 1, total_tokens: null, prompt_tokens: null, completion_tokens: null},
                 {name: 'known-zero', key_id: 50, credit: 0, requests: 0, total_tokens: 0, prompt_tokens: 10, completion_tokens: 20}];
  const tree = compiled.exports.BreakdownPanel({title: 'keys', icon: 'key-icon', items, currentKeys: true, retainedLogs: true});
  const rows = nodes(tree).filter(node => node.type === 'tr').slice(1);
  assert.equal(rows.length, 3);
  assert.equal(nodes(rows[0]).filter(node => node.type === 'td')[2].props.children, 'tokens:190');
  assert.equal(nodes(rows[1]).filter(node => node.type === 'td')[2].props.children, '—');
  assert.equal(nodes(rows[2]).filter(node => node.type === 'td')[2].props.children, 'tokens:0');
  assert.equal(nodes(rows[0]).filter(node => node.type === 'td')[5].props.children, '1');
  assert.equal(nodes(rows[1]).filter(node => node.type === 'td')[5].props.children, '—');
  assert.equal(nodes(rows[2]).filter(node => node.type === 'td')[5].props.children, '0');
  assert.match(JSON.stringify(tree), /stats.retainedKeyLogsHint/);
  assert.equal(JSON.stringify(tree).includes('NaN'), false);
});

test('realm help belongs to the selected platform, never to another provider', () => {
  const code = ts.transpileModule(fs.readFileSync(path.join(root, 'components/common/layout/RealmToggle.tsx'), 'utf8'),
    {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  for (const platform of ['workbuddy', 'qoder', 'trae']) {
    const compiled = {exports: {}}, jsx = (type, props) => ({type, props});
    const mocks = {
      'react/jsx-runtime': {jsx, jsxs: jsx}, 'lucide-react': {Globe: 'globe', House: 'house'},
      'motion/react': {motion: {span: 'span'}}, '@/lib/realm-context': {useRealm: () => ({realm: 'global', setRealm() {}})},
      '@/lib/platform-context': {usePlatform: () => ({platform})}, '@/lib/i18n/provider': {useT: () => key => key},
      '@/lib/i18n/rich-text': {RichText: 'rich-text'}, '@/lib/utils': {cn: (...parts) => parts.join(' ')},
    };
    vm.runInNewContext(code, {module: compiled, exports: compiled.exports, require: name => {assert.ok(name in mocks, name); return mocks[name];}});
    const toggle = nodes(compiled.exports.RealmToggle({})).filter(node => node.type === 'button');
    const note = JSON.stringify(compiled.exports.RealmNote({}));
    assert.equal(toggle.length, 2);
    if (platform === 'workbuddy') {assert.equal(toggle[0].props.title, 'realm.cnTitle'); assert.match(note, /realm.note/);}
    else if (platform === 'qoder') {assert.equal(toggle[0].props.title, 'Qoder · realm.cn'); assert.match(note, /realm.qoderNote/);}
    else {assert.equal(toggle[1].props.title, 'addAccount.traeGlobalUnsupported'); assert.match(note, /traeGlobalUnsupported/);}
  }
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    assert.ok(JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json'))).realm.qoderNote);
  }
});
