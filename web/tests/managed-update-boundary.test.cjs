const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const relative = 'components/common/settings/UpdatePanel.tsx';
const source = fs.readFileSync(path.join(root, relative), 'utf8');
const compiled = ts.transpileModule(source, {fileName: relative, compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
}, reportDiagnostics: true});
assert.deepEqual(compiled.diagnostics, []);

function render(product, status = {}, isAdmin = true) {
  let stateIndex = 0;
  const jsx = (type, props) => ({type, props});
  const component = name => ({[name]: name});
  const mocks = {
    react: {
      useState(initial) {
        const value = stateIndex++ === 0 ? {versions: {product}, status} : initial;
        return [value, () => {}];
      },
      useCallback: callback => callback, useEffect() {}, useRef: value => ({current: value}),
    },
    'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': new Proxy({}, {get: (_target, name) => name}),
    '@/lib/api': {errText: String, systemApi: {}},
    '@/lib/product': {PRODUCT_NAME: '智汇网关', PRODUCT_REPOSITORY: 'synthetic/zhihui',
      PRODUCT_REPOSITORY_URL: 'https://github.com/synthetic/zhihui'},
    '@/lib/toast': {notify: {}}, '@/lib/format': {fmtDateTime: String},
    '@/lib/auth-context': {useAuth: () => ({isAdmin})},
    '@/components/ui/button': component('Button'), '@/components/ui/badge': component('Badge'),
    '@/components/ui/input': component('Input'), '@/components/ui/label': component('Label'),
    '@/components/common/layout/ConfirmDialog': component('ConfirmDialog'),
    '@/components/common/states/LoadError': component('LoadError'),
    '@/lib/i18n/provider': {useT: () => key => key},
  };
  const compiledModule = {exports: {}};
  vm.runInNewContext(compiled.outputText, {module: compiledModule, exports: compiledModule.exports,
    require(name) { assert.ok(name in mocks, name); return mocks[name]; }}, {filename: relative});
  return compiledModule.exports.UpdatePanel();
}

function collect(tree, match) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(child => collect(child, match));
  return [...(match(tree) ? [tree] : []), ...collect(tree.props?.children, match)];
}

test('every identity uses the Zhihui release center, never an inherited installer', () => {
  for (const product of [undefined, {managed: true}, {managed: false, web_update_enabled: true},
    {repository: 'foreign/project', managed: false, web_update_enabled: true}]) {
    const tree = render(product);
    assert.equal(collect(tree, node => node.props?.['data-slot'] === 'trusted-release-policy').length, 1);
    assert.equal(collect(tree, node => node.props?.['data-slot'] === 'update-target').length, 0);
    assert.equal(collect(tree, node => node.props?.['data-slot'] === 'offline-update-package').length, 0);
    assert.equal(collect(tree, node => node.type === 'input' && node.props.type === 'file').length, 0);
  }
  assert.doesNotMatch(source, /systemApi\.(startUpdate|uploadPackage|deletePackage|packageMeta)/);
  assert.doesNotMatch(source, /setInterval|log_tail|targetBoth|targetManager|updatePanel\./);
});

test('old updater commands cannot masquerade as current release results', () => {
  const tree = render({repository: 'synthetic/zhihui', version: '0.0.7'}, {
    version: 'v1.0.79', ok: true, logs: [{text: 'legacy-command-NEVER-SHOW'}],
    log_tail: 'another-command-NEVER-SHOW', legacy_history_available: true,
  });
  const text = JSON.stringify(tree);
  assert.doesNotMatch(text, /v1\.0\.79|NEVER-SHOW/);
  assert.match(text, /receiptUnknown/);
  const dialogs = collect(tree, node => node.type === 'ConfirmDialog');
  assert.equal(dialogs.length, 1);
  assert.equal(dialogs[0].props.confirmText, 'releaseCenter.archiveHistory');
});

test('only matching structured phases are shown and viewers never see publisher events', () => {
  const product = {repository: 'synthetic/zhihui', version: '0.0.7'};
  const status = {product, record_state: 'reported', logs: [{ts: 1, text: 'activated'}, {ts: 2, text: 'arbitrary-shell'}]};
  assert.match(JSON.stringify(render(product, status)), /phase\.activated/);
  assert.doesNotMatch(JSON.stringify(render(product, status)), /arbitrary-shell/);
  assert.doesNotMatch(JSON.stringify(render(product, status, false)), /phase\.activated/);
  assert.doesNotMatch(JSON.stringify(render({repository: 'foreign/project'}, status)), /phase\.activated/);
  assert.doesNotMatch(JSON.stringify(render(product, {...status, product: {repository: 'foreign/project'}})), /phase\.activated/);
});

test('release links are owned by Zhihui, not receipt or cached URLs', () => {
  const links = collect(render({repository: 'synthetic/zhihui', version: '0.0.7'}), node => node.type === 'a');
  links.forEach(node => assert.match(node.props.href, /^https:\/\/github\.com\/synthetic\/zhihui(?:\/releases)?$/));
  const changelog = fs.readFileSync(path.join(root, 'components/common/settings/ChangelogPanel.tsx'), 'utf8');
  assert.match(changelog, /PRODUCT_REPOSITORY_URL/);
  assert.doesNotMatch(changelog, /ithtelab\/workbuddy-manager/);
});
