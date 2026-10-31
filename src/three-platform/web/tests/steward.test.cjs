'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

function render(file, name, data, isAdmin = true, state = []) {
  const compiled = {exports: {}}, calls = [];
  let cursor = 0;
  const values = state.slice();
  const jsx = (type, props) => ({type, props});
  const mocks = {
    react: {useEffect() {}, useRef: value => ({current: value}), useState: value => {
      const index = cursor++;
      if (!(index in values)) values[index] = value;
      return [values[index], next => {values[index] = typeof next === 'function' ? next(values[index]) : next;}];
    }},
    'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': new Proxy({}, {get: (_, key) => `icon:${key}`}),
    '@/lib/auth-context': {useAuth: () => ({isAdmin})},
    '@/lib/i18n/provider': {useI18n: () => ({t: key => key})},
    '@/lib/use-async-data': {useAsyncAll: () => ({values: {}, errors: {}, refresh: async () => {}, reload: async () => {}, ...data})},
    '@/lib/use-heartbeat': {useHeartbeat() {}},
    '@/lib/api': {errText: error => String(error)},
    '@/lib/operational-evidence': {evidenceAccess: error => !error ? null :
      [401, 403].includes(error.response?.status) ? 'forbidden' : [404, 501].includes(error.response?.status) ? 'unavailable' : 'stale'},
    '@/lib/steward': {stewardApi: new Proxy({}, {get: (_, key) => (...args) => {
      calls.push({key, args});
      if (data.api?.[key]) return data.api[key](...args);
      return Promise.resolve(key === 'save' ? {policy: {...args[0], revision: args[0].revision + 1}} : {});
    }})},
  };
  const helper = {exports: {}};
  vm.runInNewContext(ts.transpileModule(read('lib/steward-ui.ts'), {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }}).outputText, {exports: helper.exports, module: helper});
  mocks['@/lib/steward-ui'] = helper.exports;
  mocks['@/components/animate-ui/radix/dialog'] = Object.fromEntries(
    ['Dialog', 'DialogContent', 'DialogDescription', 'DialogHeader', 'DialogTitle'].map(name => [name, name]));
  for (const [module, symbol] of [['button', 'Button'], ['textarea', 'Textarea'], ['input', 'Input'], ['label', 'Label'], ['switch', 'Switch']]) {
    mocks[`@/components/ui/${module}`] = {[symbol]: symbol.toLowerCase()};
  }
  const source = read(file).replace('function AlertSettings(', 'export function AlertSettings(');
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports: compiled.exports, module: compiled, AbortController,
    require(dependency) {assert.ok(dependency in mocks, dependency); return mocks[dependency];}}, {filename: file});
  const rerender = () => {cursor = 0; return compiled.exports[name](data.props);};
  return {tree: rerender(), calls, values, rerender, helper: helper.exports};
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
    assert.equal(nodes(tree).filter(node => node.props?.id === 'steward-new-email')[0].props.disabled, true);
    assert.doesNotMatch(JSON.stringify(tree), /private@example.com/);
    assert.equal(calls.length, 0);
  }
});

test('chat transcript is bounded and scrollable; composer cannot grow the page', () => {
  const {tree} = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {values: {status}});
  const log = nodes(tree).find(node => node.props?.role === 'log');
  assert.match(log.props.className, /h-\[clamp\(240px,42dvh,440px\)\]/);
  assert.match(log.props.className, /overflow-y-auto overscroll-contain/);
  assert.equal(log.props.tabIndex, 0);
  assert.match(nodes(tree).find(node => node.props?.id === 'steward-question').props.className, /resize-none/);
});

test('recipient dialog validates one address, rejects duplicates and enforces the ten-address limit', async () => {
  const settings = {policy: {revision: 1, recipients: ['existing@example.com']}, can_edit: true,
    recipient_count: 1, capabilities: {smtp_configured: true}};
  const component = render('components/common/settings/AlertsPanel.tsx', 'AlertSettings', {
    props: {settings, reload: async () => {}, unavailable: false},
  }, true);
  assert.equal(component.helper.recipientProblem('new@example.com,other@example.com', []), 'invalid');
  assert.equal(component.helper.recipientProblem('EXISTING@EXAMPLE.COM', settings.policy.recipients), 'duplicate');
  assert.equal(component.helper.recipientProblem('new@example.com', Array.from({length: 10}, (_, i) => `a${i}@example.com`)), 'limit');
  assert.equal(component.helper.recipientProblem('User@Example.COM', []), null);
  assert.equal(component.helper.normalizeRecipient(' User@Example.COM '), 'User@example.com');
  assert.equal(nodes(component.tree).filter(node => node.type === 'textarea').length, 0);
  nodes(component.tree).find(node => node.props?.id === 'steward-new-email').props.onChange({target: {value: 'User@Example.COM'}});
  let tree = component.rerender();
  await nodes(tree).filter(node => node.type === 'form')[1].props.onSubmit({preventDefault() {}});
  tree = component.rerender();
  assert.match(JSON.stringify(tree), /User@example.com/);
  assert.equal(component.calls.length, 0, 'Adding to draft must not mutate server state');
  assert.equal(nodes(tree).find(node => node.props?.children?.[1] === 'steward.testMail')?.props.disabled, true);
  await nodes(tree).filter(node => node.type === 'form')[0].props.onSubmit({preventDefault() {}});
  assert.equal(component.calls[0].key, 'save');
  assert.deepEqual(Array.from(component.calls[0].args[0].recipients), ['existing@example.com', 'User@example.com']);
  assert.equal(component.values[0].revision, 2);
  assert.equal(component.values[7], '', 'A confirmed save must not become a client-side failure');
});

test('conversation carries bounded long assistant history without downgrading it to the user limit', () => {
  const {helper} = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {values: {status}});
  const history = helper.conversationHistory(Array.from({length: 12}, (_, i) => ({
    role: i % 2 ? 'assistant' : 'user', content: 'x'.repeat(7000),
  })));
  assert.equal(history.length, 8);
  assert.equal(history[0].content.length, 2000);
  assert.equal(history[1].content.length, 6000);
  const sanitized = helper.conversationHistory([{role: 'assistant', content: 'answer',
    evidence: {model: 'fixture', tools: [], observed_at: 1}}]);
  assert.deepEqual(Object.keys(sanitized[0]), ['role', 'content']);
});

test('failed chat restores the draft and prior history instead of leaving a false unanswered turn', async () => {
  const previous = [{role: 'user', content: 'previous'}, {role: 'assistant', content: 'prior answer'}];
  const component = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {
    values: {status}, api: {chat: async () => {throw new Error('fixture failure');}},
  }, true, ['new question', previous, false, '']);
  await nodes(component.tree).find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
  assert.equal(component.values[0], 'new question');
  assert.equal(component.values[1], previous);
  assert.equal(component.values[2], false);
  assert.match(component.values[3], /fixture failure/);
  assert.equal(component.calls.length, 1);
});

test('ability cards reflect the exact backend allowlist, never invented administrative powers', () => {
  const {tree} = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {
    values: {status: {...status, allowed_tools: ['platform_pools', 'release_and_backup']}},
  });
  const text = JSON.stringify(tree);
  assert.match(text, /steward.tools.platform_pools/);
  assert.match(text, /steward.tools.release_and_backup/);
  assert.doesNotMatch(text, /steward.tools.shell|steward.tools.restart/);
  assert.match(text, /steward.abilitiesBoundary/);
});

test('demotion hides a draft email immediately, even before cleanup effects run', () => {
  const settings = {policy: {revision: 1, recipients: ['private@example.com']}, can_edit: false,
    recipient_count: 1, capabilities: {smtp_configured: true}};
  const {tree} = render('components/common/settings/AlertsPanel.tsx', 'AlertSettings', {
    props: {settings, reload: async () => {}, unavailable: false},
  }, false, [settings.policy, settings.policy.recipients, true, 'unsaved-private@example.com']);
  assert.doesNotMatch(JSON.stringify(tree), /unsaved-private@example.com|private@example.com/);
  assert.equal(nodes(tree).find(node => node.type === 'Dialog').props.open, false);
});

test('a saved alert policy stays saved when its subsequent refresh fails', async () => {
  const settings = {policy: {revision: 1, recipients: ['existing@example.com']}, can_edit: true,
    recipient_count: 1, capabilities: {smtp_configured: true}};
  for (const reload of [async () => false, async () => {throw new Error('fixture refresh failed');}]) {
    const component = render('components/common/settings/AlertsPanel.tsx', 'AlertSettings', {
      props: {settings, reload, unavailable: false},
    });
    await nodes(component.tree).find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
    assert.equal(component.values[0].revision, 2);
    assert.equal(component.values[6], 'steward.savedRefreshFailed');
    assert.equal(component.values[7], '');
    assert.deepEqual(component.calls.map(call => call.key), ['save']);
  }
});

test('demotion immediately masks retained conversation drafts and errors', () => {
  const {tree} = render('components/common/settings/StewardPanel.tsx', 'StewardPanel', {values: {status}}, false,
    ['private question', [{role: 'assistant', content: 'private answer'}], false, 'private error']);
  assert.doesNotMatch(JSON.stringify(tree), /private question|private answer|private error/);
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
