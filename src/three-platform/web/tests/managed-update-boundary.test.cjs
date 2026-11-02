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

function render(versionProduct, statusProduct, present) {
  let stateIndex = 0;
  const jsx = (type, props) => ({type, props});
  const component = name => ({[name]: name});
  const mocks = {
    react: {
      useState(initial) {
        const index = stateIndex++;
        const value = index === 0 ? {product: statusProduct, running: false, ok: null, logs: []}
          : index === 2 ? {product: versionProduct}
          : index === 6 ? {present, name: 'inert.tar.gz'} : initial;
        return [value, () => {}];
      },
      useCallback: callback => callback, useEffect() {}, useMemo: callback => callback(),
      useRef: value => ({current: value}),
    },
    'react/jsx-runtime': {jsx, jsxs: jsx},
    'lucide-react': new Proxy({}, {get: (_, name) => name}),
    '@/lib/api': {errText: String, systemApi: {}},
    '@/lib/product': {PRODUCT_REPOSITORY: 'synthetic/repository', PRODUCT_REPOSITORY_URL: 'https://invalid.example'},
    '@/lib/toast': {notify: {}}, '@/lib/format': {fmtAgo: String, fmtDateTime: String},
    '@/lib/auth-context': {useAuth: () => ({isAdmin: true})},
    '@/components/ui/button': component('Button'), '@/components/ui/badge': component('Badge'),
    '@/components/ui/input': component('Input'), '@/components/ui/label': component('Label'),
    '@/components/common/layout/ConfirmDialog': component('ConfirmDialog'),
    '@/components/common/states/LoadError': component('LoadError'),
    '@/lib/i18n/rich-text': component('RichText'), '@/lib/i18n/provider': {useT: () => key => key},
  };
  const module = {exports: {}};
  vm.runInNewContext(compiled.outputText, {module, exports: module.exports,
    require(name) { assert.ok(name in mocks, name); return mocks[name]; }}, {filename: relative});
  return module.exports.UpdatePanel();
}

function collect(tree, match) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(child => collect(child, match));
  return [...(match(tree) ? [tree] : []), ...collect(tree.props?.children, match)];
}

const managed = {managed: true, web_update_enabled: false};
const legacy = {managed: false, web_update_enabled: true};

test('managed, unknown and conflicting identities disable every package control', () => {
  for (const [versions, status] of [[managed, managed], [undefined, undefined],
    [legacy, managed], [managed, legacy], [legacy, undefined],
    [{managed: false, web_update_enabled: false}, legacy]]) {
    for (const present of [false, true]) {
      const tree = render(versions, status, present);
      const [fieldset] = collect(tree, node => node.props?.['data-slot'] === 'offline-update-package');
      assert.ok(fieldset);
      assert.equal(fieldset.type, 'fieldset');
      assert.equal(fieldset.props.disabled, true);
      const hints = collect(fieldset, node => node.type === 'RichText');
      assert.equal(hints[0].props.text, 'product.controlledDetail');
      const [targets] = collect(tree, node => node.props?.['data-slot'] === 'update-target');
      const controls = collect(targets, node => node.type === 'ConfirmDialog');
      assert.equal(controls.length, 2);
      controls.forEach(node => assert.equal(node.props.trigger.props.disabled, true));
    }
  }
});

test('explicitly unmanaged identities preserve historical package controls', () => {
  const [fieldset] = collect(render(legacy, legacy, false),
    node => node.props?.['data-slot'] === 'offline-update-package');
  assert.equal(fieldset.props.disabled, false);
  assert.equal(collect(fieldset, node => node.type === 'RichText')[0].props.text, 'updatePanel.pkgDesc');
  assert.equal(collect(fieldset, node => node.type === 'input' && node.props.type === 'file').length, 1);
});
