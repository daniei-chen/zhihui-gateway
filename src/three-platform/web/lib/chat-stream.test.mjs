/**
 * K31 生命周期测试：`streamChat`（lib/chat-stream.ts）。
 *
 * 跑法：`cd src/three-platform/web && node --test lib/*.test.mjs`
 *
 * 用**可断言的假 fetch / 假 reader**，所以能验证的真不只是「正文拼出来了」：
 *   · 一次调用只发一次请求（`calls.requests` 恒为 1，出错也不重试）；
 *   · 正常 EOF → 只 releaseLock；取消/读失败 → 先 cancel 再 releaseLock；
 *   · `aborted`（用户点停止）与 `truncated`（上游没发 [DONE] 就断）分开；
 *   · 错误帧、HTTP 错误、非法 JSON、超长正文各走各的分支，不互相污染。
 *
 * 这些行为在浏览器里都**不会报错**，只会在断路/切页时表现为「一直转圈」「回答
 * 突然停住却像正常结束」或者「同一句话被扣两次钱」，所以必须锁死。
 */
import {streamChat} from './chat-stream.ts';

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

const enc = new TextEncoder();
const sse = (obj) => `data: ${typeof obj === 'string' ? obj : JSON.stringify(obj)}\n\n`;
const delta = (text) => sse({choices: [{delta: {content: text}}]});

/**
 * 构造假的流 + 假 fetch。
 * @param {object} opts chunks 分片（Uint8Array 或字符串，字符串会被编码）
 * @param {number} opts.rejectAt 第几次 read 抛错（-1 表示不抛）
 * @param {Error} opts.reject 抛出的错误
 */
function makeStream({chunks = [], rejectAt = -1, reject = null, status = 200, json, text} = {}) {
  const calls = {reads: 0, cancels: 0, releases: 0, requests: 0};
  const bytes = chunks.map((c) => (typeof c === 'string' ? enc.encode(c) : c));
  let i = 0;
  const reader = {
    async read() {
      calls.reads += 1;
      if (rejectAt >= 0 && i === rejectAt) throw reject;
      if (i < bytes.length) return {done: false, value: bytes[i++]};
      return {done: true};
    },
    async cancel() {
      calls.cancels += 1;
    },
    releaseLock() {
      calls.releases += 1;
    },
  };
  const res = {
    ok: status >= 200 && status < 300,
    status,
    body: status >= 200 && status < 300 ? {getReader: () => reader} : null,
    json: json ? async () => json : undefined,
    text: text ? async () => text : undefined,
  };
  const fetchImpl = async () => {
    calls.requests += 1;
    return res;
  };
  return {calls, fetchImpl};
}

const base = {url: '/api/playground/chat', body: {model: 'x'}};

console.log('正常终态：');
{
  const {calls, fetchImpl} = makeStream({
    chunks: [
      delta('你'),
      delta('好'),
      sse({usage: {prompt_tokens: 3, completion_tokens: 5, credit: 0.25}}),
      sse('[DONE]'),
    ],
  });
  const deltas = [];
  const usages = [];
  const r = await streamChat({
    ...base,
    fetchImpl,
    onDelta: (d) => deltas.push(d),
    onUsage: (u) => usages.push(u),
  });
  check('answer concatenated in order', r.answer, '你好');
  check('sawDone true', r.sawDone, true);
  check('not truncated', r.truncated, false);
  check('not aborted', r.aborted, false);
  check('no error', r.error, null);
  check('http status kept', r.httpStatus, 200);
  check('usage credit from stream', r.usage.credit, 0.25);
  check('usage tokens = prompt + completion', r.usage.tokens, 8);
  check('onDelta received each delta', deltas, ['你', '好']);
  check('onUsage called', usages.length, 1);
  check('exactly one request (no duplicate POST / no double billing)', calls.requests, 1);
  check('natural EOF releases lock', calls.releases, 1);
  check('natural EOF does not cancel reader', calls.cancels, 0);
}

console.log('协议终态与截断：');
{
  // 没有 [DONE]，流自然结束：必须标 truncated，但已收到的正文要保留
  const {calls, fetchImpl} = makeStream({chunks: [delta('半'), delta('截')]});
  const r = await streamChat({...base, fetchImpl});
  check('answer preserved on truncation', r.answer, '半截');
  check('truncated when [DONE] never arrives', r.truncated, true);
  check('not aborted (server closed)', r.aborted, false);
  check('truncation is not an error', r.error, null);
  check('truncated: reader released', calls.releases, 1);
  check('truncated: no cancel needed (EOF reached)', calls.cancels, 0);
}
{
  // 半截关闭：最后一个事件只有 data 行、没有结束空行 → flush 仍要派发
  const {fetchImpl} = makeStream({chunks: ['data: ' + JSON.stringify({choices: [{delta: {content: 'tail'}}]})]});
  const r = await streamChat({...base, fetchImpl});
  check('half-closed last event still parsed', r.answer, 'tail');
  check('half-closed marked truncated', r.truncated, true);
}
{
  // [DONE] 之后上游又发了一帧（有的实现会补发 usage）：不能重复计入正文
  const {fetchImpl} = makeStream({chunks: [delta('A'), sse('[DONE]'), delta('B')]});
  const r = await streamChat({...base, fetchImpl});
  check('[DONE] is terminal for content', r.answer, 'A');
}

console.log('取消：');
{
  const abortErr = new DOMException('The operation was aborted.', 'AbortError');
  const {calls, fetchImpl} = makeStream({chunks: [delta('已收到')], rejectAt: 1, reject: abortErr});
  const r = await streamChat({...base, fetchImpl});
  check('aborted flagged', r.aborted, true);
  check('aborted is not truncated', r.truncated, false);
  check('aborted keeps partial answer', r.answer, '已收到');
  check('aborted is not an error', r.error, null);
  check('aborted: reader cancelled', calls.cancels, 1);
  check('aborted: lock released', calls.releases, 1);
  check('aborted: still exactly one request', calls.requests, 1);
}
{
  // 还没发就先取消：一次请求都不该发（切平台时常见的竞态）
  const ac = new AbortController();
  ac.abort();
  let requests = 0;
  const r = await streamChat({
    ...base,
    signal: ac.signal,
    fetchImpl: async () => {
      requests += 1;
      return {ok: true, status: 200, body: null};
    },
  });
  check('pre-aborted signal sends no request', requests, 0);
  check('pre-aborted flagged aborted', r.aborted, true);
}

console.log('错误分支：');
{
  // 错误帧（OpenAI 风格）：要取出可展示的 message/code，且不打断已收到的正文
  const {calls, fetchImpl} = makeStream({
    chunks: [delta('前半'), sse({error: {message: 'upstream overloaded', code: 11102}})],
  });
  const r = await streamChat({...base, fetchImpl});
  check('error frame captured', r.error, 'upstream overloaded (11102)');
  check('error frame keeps earlier answer', r.answer, '前半');
  check('error frame not truncated', r.truncated, false);
  check('error frame still cleans reader', calls.releases, 1);
}
{
  // HTTP 非 2xx：读 detail，不取 reader，不重试
  const {calls, fetchImpl} = makeStream({status: 502, json: {detail: '上游暂不可用'}});
  const r = await streamChat({...base, fetchImpl});
  check('http error detail surfaced', r.error, '上游暂不可用');
  check('http status recorded', r.httpStatus, 502);
  check('http error does not retry', calls.requests, 1);
  check('http error never touches reader', [calls.reads, calls.cancels, calls.releases], [0, 0, 0]);
}
{
  // 读流中途失败（网络断开）：要 cancel 释放连接，并给出错误说明
  const {calls, fetchImpl} = makeStream({chunks: [delta('x')], rejectAt: 1, reject: new Error('network down')});
  const r = await streamChat({...base, fetchImpl});
  check('read failure surfaced', r.error, 'network down');
  check('read failure cancels reader', calls.cancels, 1);
  check('read failure releases lock', calls.releases, 1);
}
{
  // 非法 JSON 帧：丢弃并计数，正文不受影响
  const {fetchImpl} = makeStream({chunks: ['data: not-json\n\n', delta('ok'), sse('[DONE]')]});
  const r = await streamChat({...base, fetchImpl});
  check('invalid frame ignored, counted', r.ignoredEvents, 1);
  check('invalid frame does not break stream', [r.answer, r.sawDone], ['ok', true]);
}
{
  // 正文超限：停止读取并报错（不能无限吃内存）
  const {calls, fetchImpl} = makeStream({chunks: [delta('12345'), delta('67890'), sse('[DONE]')]});
  const r = await streamChat({...base, maxAnswerChars: 6, fetchImpl});
  check('oversized answer flagged', typeof r.error, 'string');
  check('oversized answer stops reading', r.answer.length >= 6, true);
  check('oversized answer cancels reader', calls.cancels, 1);
}

console.log('分片与编码：');
{
  // UTF-8 拆字节 + 一行拆两片：最终正文必须完整、没有 U+FFFD
  const payload = enc.encode(delta('中文测试'));
  const split = [payload.subarray(0, 9), payload.subarray(9, 20), payload.subarray(20)];
  const {fetchImpl} = makeStream({chunks: [...split, sse('[DONE]')]});
  const r = await streamChat({...base, fetchImpl});
  check('UTF-8 split through reader', r.answer, '中文测试');
  check('no replacement char', r.answer.includes('\uFFFD'), false);
}
{
  // 一个分片里多个事件
  const {fetchImpl} = makeStream({chunks: [delta('a') + delta('b') + sse('[DONE]')]});
  const r = await streamChat({...base, fetchImpl});
  check('multiple events in one chunk', r.answer, 'ab');
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
