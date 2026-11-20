/**
 * 豆包专用文案 key 的完整性。
 *
 * 2026-11-20 真机事故：`UploadAccountsButton` 按平台切 key 时把首字母拼成了
 * 小写（`accounts.doubaoimportExportDesc`），而词典里是
 * `accounts.doubaoImportExportDesc` —— i18n 找不到就**把 key 名本身渲染到界面上**，
 * 用户看到的是「accounts.doubaoimportExportDesc」。
 *
 * 这类错误不会让任何构建或类型检查失败（key 是运行时字符串），所以用测试钉住：
 * 组件里引用的每个 `doubao*` key，都必须在**五种语言**里存在。
 */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCALES = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko'];

let failed = 0;
let passed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    passed += 1;
    return;
  }
  failed += 1;
  console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
}

/** 与组件里 `tx()` 的拼接规则必须完全一致。 */
function doubaoKey(base) {
  return `doubao${base[0].toUpperCase()}${base.slice(1)}`;
}

// 组件里通过 `tx('...')` 引用的 base key
const TX_BASES = [
  'importExportDesc',
  'importAccountDesc',
  'exportAccountDesc',
  'uploadAllowOverwrite',
];

function loadDict(locale) {
  const file = path.join(HERE, 'i18n', 'locales', `${locale}.json`);
  return JSON.parse(readFileSync(file, 'utf8'));
}

console.log('豆包专用 key：');
for (const locale of LOCALES) {
  const dict = loadDict(locale);
  const accounts = dict.accounts || {};
  for (const base of TX_BASES) {
    const key = doubaoKey(base);
    check(`${locale} has accounts.${key}`, typeof accounts[key], 'string');
    check(`${locale} accounts.${key} non-empty`, (accounts[key] || '').length > 0, true);
  }
}

console.log('原有 key 未被覆盖：');
for (const locale of LOCALES) {
  const dict = loadDict(locale);
  const accounts = dict.accounts || {};
  for (const base of TX_BASES) {
    // 豆包走独立 key，原文案必须保持原样 —— 否则 WorkBuddy / Trae 会读到豆包文案。
    check(`${locale} keeps accounts.${base}`, typeof accounts[base], 'string');
    check(`${locale} accounts.${base} is not doubao text`,
      /doubao|cookies|JSON 清单|JSON manifest/i.test(accounts[base] || ''), false);
  }
}

console.log('拼接规则：');
check('capitalizes first letter', doubaoKey('importExportDesc'), 'doubaoImportExportDesc');
check('lowercase variant would be wrong',
  doubaoKey('importExportDesc') === 'doubaoimportExportDesc', false);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
