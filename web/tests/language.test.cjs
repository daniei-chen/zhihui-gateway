'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};
const jsx = (type, props) => ({type, props});

function load(relative, mocks, globals = {}) {
  const source = fs.readFileSync(path.join(root, relative), 'utf8');
  const output = ts.transpileModule(source, {fileName: relative, compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const compiled = {exports: {}};
  vm.runInNewContext(output, {module: compiled, exports: compiled.exports, console, ...globals,
    require(name) {
      assert.ok(name in mocks, `Unexpected dependency: ${name}`);
      return mocks[name];
    },
  }, {filename: relative});
  return compiled.exports;
}

function hooks() {
  const state = [];
  let cursor = 0;
  const effects = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const react = {
    createContext: () => ({Provider: 'provider'}),
    useContext() { throw new Error('Not used in provider-only tests'); },
    useState(value) {
      const index = cursor++;
      state[index] ??= {value};
      return [state[index].value, next => {
        state[index].value = typeof next === 'function' ? next(state[index].value) : next;
      }];
    },
    useRef(value) { const index = cursor++; return state[index] ??= {current: value}; },
    useMemo(fn, deps) {
      const index = cursor++;
      if (!same(state[index]?.deps, deps)) state[index] = {value: fn(), deps};
      return state[index].value;
    },
    useCallback(fn, deps) { return react.useMemo(() => fn, deps); },
    useEffect(fn, deps) {
      const index = cursor++;
      if (!same(state[index]?.deps, deps)) {
        effects.push(() => {
          state[index]?.cleanup?.();
          state[index] = {deps, cleanup: fn()};
        });
      }
    },
  };
  return {
    react,
    render(fn) { cursor = 0; return fn(); },
    effects() { effects.splice(0).forEach(fn => fn()); },
    unmount() { state.forEach(slot => slot?.cleanup?.()); },
  };
}

function provider({target = 'en', storageThrows = false} = {}) {
  const h = hooks();
  const loaded = new Set(['zh-CN']);
  const pending = new Map();
  const persisted = new Map();
  const config = load('lib/i18n/config.ts', {});
  const document = {documentElement: {lang: 'zh-CN'}};
  const window = {localStorage: {setItem(key, value) {
    if (storageThrows) throw new Error('Storage unavailable');
    persisted.set(key, value);
  }}};
  const {I18nProvider} = load('lib/i18n/provider.tsx', {
    react: h.react, 'react/jsx-runtime': {jsx, jsxs: jsx},
    './config': {...config, detectLocale: () => target},
    './index': {setActiveLocale() {}, tp: value => value, translator: locale => key => `${locale}:${key}`},
    './load': {isLocaleLoaded: locale => loaded.has(locale), ensureLocaleDict(locale) {
      if (!pending.has(locale)) pending.set(locale, deferred());
      return pending.get(locale).promise;
    }},
    '../use-hydrated': {useHydrated: () => true},
  }, {window, document});
  return {h, persisted, document,
    render: () => h.render(() => I18nProvider({children: null})).props.value,
    async complete(locale, ok = true) {
      if (ok) loaded.add(locale);
      assert.ok(pending.has(locale));
      pending.get(locale).resolve(ok);
      await flush();
    },
  };
}

test('the visible language control is mounted exactly once beside platform and realm', () => {
  const source = fs.readFileSync(path.join(root, 'app/(main)/layout.tsx'), 'utf8');
  assert.equal((source.match(/<LanguageToggle\s*\/>/g) || []).length, 1);
  assert.match(source, /<PlatformToggle\s*\/>\s*<RealmToggle\s*\/>\s*<LanguageToggle\s*\/>/);
});

test('language options have native labels, programmatic names, and validated values', () => {
  const config = load('lib/i18n/config.ts', {});
  const chosen = [];
  const names = ['Select', 'SelectContent', 'SelectItem', 'SelectTrigger', 'SelectValue'];
  const {LanguageToggle} = load('components/common/layout/LanguageToggle.tsx', {
    'react/jsx-runtime': {jsx, jsxs: jsx}, 'lucide-react': {Languages: 'languages-icon'},
    '@/lib/i18n/config': config,
    '@/lib/i18n/provider': {useI18n: () => ({locale: 'ja', setLocale: value => chosen.push(value),
      t: key => key, locales: config.LOCALES})},
    '@/lib/utils': {cn: (...values) => values.filter(Boolean).join(' ')},
    '@/components/ui/select': Object.fromEntries(names.map(name => [name, name])),
  });
  const tree = LanguageToggle({});
  const [trigger, content] = tree.props.children;
  assert.equal(trigger.props['data-slot'], 'language-toggle');
  assert.equal(trigger.props['aria-label'], 'language.switch');
  assert.equal(trigger.props['data-locale'], 'ja');
  assert.equal(content.props.children.length, 5);
  for (const child of content.props.children) {
    assert.equal(child.props.children.props.lang, child.props.value);
    assert.equal(child.props.children.props.children, config.LOCALE_LABELS[child.props.value]);
  }
  tree.props.onValueChange('not-a-locale');
  assert.deepEqual(chosen, []);
  tree.props.onValueChange('ko');
  assert.deepEqual(chosen, ['ko']);
});

test('a late startup dictionary cannot overwrite an explicit language selection', async () => {
  const p = provider();
  p.render(); p.h.effects();
  p.render().setLocale('ja');
  assert.equal(p.render().locale, 'ja');
  await p.complete('en');
  assert.equal(p.render().locale, 'ja');
  await p.complete('ja');
  assert.equal(p.render().locale, 'ja');
  assert.equal(p.persisted.get('workbuddy-manager:locale'), 'ja');
});

test('rapid choices remain last-selection-wins with out-of-order dictionary responses', async () => {
  const p = provider({target: 'zh-CN'});
  p.render(); p.h.effects();
  p.render().setLocale('en');
  p.render().setLocale('ko');
  const before = p.render().t;
  await p.complete('en');
  assert.equal(p.render().locale, 'ko');
  assert.equal(p.render().t, before);
  await p.complete('ko');
  assert.equal(p.render().locale, 'ko');
  assert.notEqual(p.render().t, before);
});

test('a source-language choice cancels startup restoration even when already loaded', async () => {
  const p = provider();
  p.render(); p.h.effects();
  p.render().setLocale('zh-CN');
  await p.complete('en');
  assert.equal(p.render().locale, 'zh-CN');
  assert.equal(p.persisted.get('workbuddy-manager:locale'), 'zh-CN');
});

test('storage denial does not disable switching and stale completion after unmount is ignored', async () => {
  const p = provider({target: 'zh-CN', storageThrows: true});
  p.render(); p.h.effects();
  assert.doesNotThrow(() => p.render().setLocale('en'));
  assert.equal(p.render().locale, 'en');
  const before = p.render().t;
  p.h.unmount();
  await p.complete('en');
  assert.equal(p.render().t, before);
});

test('failed startup dictionary preserves source-language fallback without throwing', async () => {
  const p = provider();
  p.render(); p.h.effects();
  await p.complete('en', false);
  assert.equal(p.render().locale, 'zh-CN');
});
