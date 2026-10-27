/**
 * N14 回归测试：`backendErrorText` / `envelopeText`（lib/err-envelope.ts）。
 *
 * 背景：QTG 的 502/4xx 信封是 `{"error": {"code", "message", "type"}}`
 * （qtg/app/qtg/errors.py:119）。旧 `errText` 实现 `d?.detail || d?.error` 把这个
 * **对象**直接当文案返回，穿透到 `notify.err(...)` 的文本节点，浏览器抛 React #31
 * （objects are not valid as a React child）——设置相关页面在引擎控制面不可用
 * （fixture 里 /api/accounts、/api/upstreams 502）时整页崩。
 * 见 tasks/NEW-FINDINGS.md N14、evidence/browser/2026-10-24T05-13-02-915Z-f85f6f2c/。
 *
 * 每条断言都在钉「返回值必须是 string」——这是渲染层的合同。
 */
import {backendErrorText, envelopeText} from './err-envelope.ts';

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

// —— QTG 结构化信封（触发 N14 的实际形状）——
check(
  'envelope: error.message extracted',
  backendErrorText({error: {code: 'upstream_error', message: '上游控制面不可用：qoder', type: 'api_error'}}),
  '上游控制面不可用：qoder',
);
check(
  'envelope without message -> no text (caller falls back), never the object',
  backendErrorText({error: {code: 'x', type: 'api_error'}}),
  '',
);

// —— 既有形状不回归 ——
check('plain string body passes through', backendErrorText('服务暂时不可用'), '服务暂时不可用');
check('empty string body -> no text', backendErrorText(''), '');
check('detail string wins over error', backendErrorText({detail: '详情文案', error: '错误文案'}), '详情文案');
check('error string used when no detail', backendErrorText({error: '错误文案'}), '错误文案');
check('nested detail object with message', backendErrorText({detail: {message: '嵌套详情'}}), '嵌套详情');

// —— FastAPI 校验错误：detail 是对象数组，必须判「无文案」而不是漏数组 ——
check(
  'detail array (FastAPI validation) -> empty, not the array',
  backendErrorText({detail: [{loc: ['body', 'name'], msg: 'field required', type: 'missing'}]}),
  '',
);

// —— 非对象输入 ——
check('null body -> empty', backendErrorText(null), '');
check('number body -> empty', backendErrorText(42), '');
check('array body without messages -> empty', backendErrorText([1, 2, 3]), '');

// —— envelopeText 单元行为 ——
check('envelopeText: string as-is', envelopeText('x'), 'x');
check('envelopeText: object.message', envelopeText({message: 'm'}), 'm');
check('envelopeText: object.message non-string ignored', envelopeText({message: 9}), '');
check('envelopeText: array ignored', envelopeText([1]), '');

// —— 合同：任何输入都不允许非字符串返回值 ——
const hostile = [
  {error: {code: 'a', message: {nested: true}, type: 't'}},
  {detail: {message: 42}, error: [1, 2]},
  {detail: undefined, error: undefined},
  {},
  {error: null},
  [[[{}]]],
];
for (const [i, d] of hostile.entries()) {
  const r = backendErrorText(d);
  check(`hostile input #${i} returns a string`, typeof r === 'string' ? 'string' : typeof r, 'string');
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
