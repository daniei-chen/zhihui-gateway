'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

function render(file, name, data, isAdmin = true) {
  const compiled = {exports: {}}, calls = [];
  const jsx = (type, props) => ({type, props});
  const mocks = {
    react: {useEffect() {}, useRef: value => ({current: value}), useState: value => [value, () => {}]},
    'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': new Proxy({}, {get: (_, key) => `icon:${key}`}),
    '@/lib/auth-context': {useAuth: () => ({isAdmin})},
    '@/lib/i18n/provider': {useI18n: () => ({t: key => key})},
    '@/lib/use-async-data': {useAsyncAll: () => ({values: {}, errors: {}, refresh: async () => {}, reload: async () => {}, ...data})},
    '@/lib/use-heartbeat': {useHeartbeat() {}},
    '@/lib/api': {errText: error => String(error)},
    '@/lib/operational-evidence': {evidenceAccess: error => !error ? null :
      [401, 403].includes(error.response?.status) ? 'forbidden' : [404, 501].includes(error.response?.status) ? 'unavailable' : 'stale'},
    '@/lib/steward': {stewardApi: new Proxy({}, {get: (_, key) => (...args) => {calls.push({key, args}); return Promise.resolve({});}})},
  };
  for (const [module, symbol] of [['button', 'Button'], ['textarea', 'Textarea'], ['input', 'Input'], ['label', 'Label'], ['switch', 'Switch']]) {
    mocks[`@/components/ui/${module}`] = {[symbol]: symbol.toLowerCase()};
  }
  const source = read(file).replace('function AlertSettings(', 'export function AlertSettings(');
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports: compiled.exports, module: compiled, AbortController,
    require(dependency) {assert.ok(dependency in mocks, dependency); return mocks[dependency];}}, {filename: file});
  return {tree: compiled.exports[name](data.props), calls};
}
function nodes(tree) {
  const result = [];
  function visit(node) {
    if (Array.isArray(node)) {node.forEach(visit); return;}
    if (!node || typeof node !== 'object') return;
    result.push(node); visit(node.props?.children);
  }
  visit(tree); return result;
}
const status = {available: true, stale: false, can_chat: true, capabilities: {model_configured: true},
  snapshot: {platforms: [{name: 'qoder', state: 'up', pool: {total: 987}}]}};

test('steward permits only current super-admin metered analysis with fresh evidence', async () => {
  for (const [isAdmin, current, blocked] of [[true, status, false], [false, status, true],
    [true, {...status, stale: true}, true], [true, {...status, can_chat: false}, true],
    [true, {...status, capabilities: {model_configured: false}}, true]]) {
    const {tree, calls} = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {values: {status: current}}, isAdmin);
    assert.equal(nodes(tree).find(node => node.props?.id === 'steward-question').props.disabled, blocked);
    assert.equal(calls.length, 0, 'Rendering must never generate model requests');
    if (blocked) {
      await nodes(tree).find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
      assert.equal(calls.length, 0);
    }
  }
});

test('steward hides retained snapshot on permission/unavailable errors; network failure is marked stale', () => {
  for (const code of [401, 403, 404, 501, 503]) {
    const {tree} = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {
      values: {status}, errors: {status: {response: {status: code}}},
    });
    const text = JSON.stringify(tree);
    assert.match(text, /steward.loadFailed/);
    if (code === 503) assert.match(text, /987/);
    else assert.doesNotMatch(text, /987/);
    assert.equal(nodes(tree).find(node => node.props?.id === 'steward-question').props.disabled, true);
  }
});

test('alerts hide retained recipients and event evidence after permission loss', () => {
  const values = {settings: {policy: {revision: 1, recipients: ['private@example.com']}, recipient_count: 1},
    alerts: {incidents: [{id: 1, state: 'open', title: 'private-event-evidence', detail: 'fixture', last_seen: 123}], deliveries: [], analyses: []}};
  for (const code of [403, 501, 503]) {
    const errors = {settings: {response: {status: code}}, alerts: {response: {status: code}}};
    const {tree, calls} = render('components/common/settings/AlertsPanel.tsx', 'AlertsPanel', {values, errors});
    const text = JSON.stringify(tree);
    assert.match(text, /steward.loadFailed/);
    if (code === 503) {assert.match(text, /private-event-evidence/); assert.match(text, /"unavailable":true/);}
    else assert.doesNotMatch(text, /private-event-evidence|private@example.com/);
    assert.equal(calls.length, 0);
  }
});

test('alert editor never renders retained recipients to a demoted administrator', () => {
  for (const [isAdmin, canEdit] of [[false, true], [true, false], [false, false]]) {
    const {tree, calls} = render('components/common/settings/AlertsPanel.tsx', 'AlertSettings', {
      props: {settings: {policy: {revision: 1, recipients: ['private@example.com']},
        can_edit: canEdit, recipient_count: 1, capabilities: {smtp_configured: true}}, reload: async () => {}, unavailable: false},
    }, isAdmin);
    assert.equal(nodes(tree).find(node => node.props?.id === 'steward-recipients').props.value, '');
    assert.doesNotMatch(JSON.stringify(tree), /private@example.com/);
    assert.equal(calls.length, 0);
  }
});

test('all steward dynamic status and analysis labels exist with matching placeholders in five languages', () => {
  const flatten = (value, prefix = '') => Object.fromEntries(Object.entries(value).flatMap(([key, item]) =>
    typeof item === 'object' ? Object.entries(flatten(item, `${prefix}${key}.`)) : [[`${prefix}${key}`, item]]));
  let baseline;
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const data = JSON.parse(read(`lib/i18n/locales/${locale}.json`));
    assert.ok(data.settings.tabSteward); assert.ok(data.settings.tabAlerts);
    const strings = flatten(data.steward);
    if (!baseline) baseline = strings;
    assert.deepEqual(Object.keys(strings).sort(), Object.keys(baseline).sort(), locale);
    for (const [key, value] of Object.entries(strings)) {
      assert.ok(typeof value === 'string' && value.trim(), `${locale}:${key}`);
      assert.deepEqual(value.match(/\{\w+\}/g) || [], baseline[key].match(/\{\w+\}/g) || [], `${locale}:${key}`);
    }
    for (const phase of ['pending', 'running', 'complete', 'cancelled', 'failed', 'unknown']) assert.ok(strings[`analysis.${phase}`]);
    assert.ok(strings.analysisRecords); assert.ok(strings.analysisBoundary);
  }
});
