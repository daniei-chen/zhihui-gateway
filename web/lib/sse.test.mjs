/**
 * K31 协议层测试：`createSseParser`（lib/sse.ts）。
 *
 * 跑法（Node ≥ 22.6，本项目实测 v24.19）：
 *     cd src/three-platform/web && node --test lib/*.test.mjs
 *
 * 为什么每条用例都值得留：这些边界在浏览器里**大多看不出来**——
 *   · UTF-8 拆字节失败 → 回答里出现 U+FFFD（���），不报错；
 *   · CRLF 处理不对 → 同一行被当成两行，事件提前派发或永远不派发；
 *   · 多行 data 不拼接 → 上游把一条 JSON 折成两行时整条事件被丢；
 *   · 半截关闭不标记 → 界面上就是「回答突然停住但像正常结束」；
 *   · 空 data 也派发 → 与 keepalive 混淆，界面多出空消息。
 * 原实现只能靠肉眼看流，所以每一条都用断言钉住。
 */
import {createSseParser} from './sse.ts';

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
const feed = (parser, chunks) => {
  const out = [];
  for (const c of chunks) out.push(...parser.push(c));
  return out;
};

console.log('基本事件：');
{
  const p = createSseParser();
  const evs = feed(p, ['data: {"a":1}\n\n']);
  check('single event dispatched', evs.length, 1);
  check('default event name is message', evs[0].event, 'message');
  check('data kept verbatim', evs[0].data, '{"a":1}');
  check('no truncation when closed by blank line', p.flush().length + p.truncated, 0);
}

console.log('行结束符：');
{
  const p = createSseParser();
  const evs = feed(p, ['data: a\r\n\r\n']);
  check('CRLF: one event', evs.map((e) => e.data), ['a']);
  check('CRLF: not truncated', p.flush().length === 0 && !p.truncated, true);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data: b\r\r']);
  // 结尾那个 CR 可能是下一个分片 "\n" 的前半（CRLF 拆包），所以先挂起一行；
  // 真正的「流结束」由 flush 确认。
  check('CR-only: trailing CR waits for the next chunk', evs.length, 0);
  check('CR-only: flush closes the event', p.flush().map((e) => e.data), ['b']);
  check('CR-only: closed by CR, not truncated', p.truncated, false);
}
{
  // CRLF 被拆在两个分片里：第一片以 \r 结尾，必须等第二片确认是不是 \r\n
  const p = createSseParser();
  const first = feed(p, ['data: c\r']);
  const second = feed(p, ['\n\r\n']);
  check('split CRLF: nothing dispatched on lone CR', first.length, 0);
  check('split CRLF: dispatched after continuation', second.map((e) => e.data), ['c']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data: d\n\r\n']);
  check('mixed LF/CRLF still one event', evs.map((e) => e.data), ['d']);
}

console.log('分片与 UTF-8：');
{
  // 「中」= E4 B8 AD，故意在第 2 个字节处切开发送
  const bytes = enc.encode('data: 中文\n\n');
  const p = createSseParser();
  const evs = [...p.push(bytes.subarray(0, 7)), ...p.push(bytes.subarray(7))];
  check('UTF-8 split across chunks decoded correctly', evs.map((e) => e.data), ['中文']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data: {"x"', ':1}\n\n']);
  check('chunk split mid-line reassembled', evs.map((e) => e.data), ['{"x":1}']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data: 1\n\ndata: 2\n\ndata: 3\n\n']);
  check('three events in one chunk', evs.map((e) => e.data), ['1', '2', '3']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data: 1\n\ndata: 2\n\n']);
  check('event count matches dispatch', p.eventCount, 2);
  check('events stay ordered', evs.map((e) => e.data), ['1', '2']);
}

console.log('多行 data / 注释 / 字段：');
{
  const p = createSseParser();
  const evs = feed(p, ['data: line1\ndata: line2\n\n']);
  check('multi-line data joined with LF (spec)', evs.map((e) => e.data), ['line1\nline2']);
}
{
  const p = createSseParser();
  const evs = feed(p, [': keepalive\n\ndata: x\n\n']);
  check('comment lines are ignored', evs.map((e) => e.data), ['x']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data:  two-spaces\n\n']);
  check('only one leading space stripped (spec)', evs.map((e) => e.data), [' two-spaces']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['event: done\nid: 42\nretry: 3000\ndata: z\n\n']);
  check('event name kept', evs[0].event, 'done');
  check('id kept', evs[0].id, '42');
  check('retry parsed as number', evs[0].retry, 3000);
}
{
  // id 跨事件保留（断线续传要它），event 名称每个事件重置
  const p = createSseParser();
  const evs = feed(p, ['id: 7\ndata: a\n\n', 'data: b\n\n']);
  check('id persists across events', [evs[0].id, evs[1].id], ['7', '7']);
  check('event name resets to message', [evs[0].event, evs[1].event], ['message', 'message']);
}
{
  const p = createSseParser();
  const evs = feed(p, ['event: ping\n\n']);
  check('event without data is not dispatched', evs.length, 0);
}
{
  const p = createSseParser();
  const evs = feed(p, ['data:\n\n']);
  check('empty data is not dispatched (keepalive)', evs.length, 0);
}

console.log('半截关闭：');
{
  const p = createSseParser();
  const evs = feed(p, ['data: {"choices":[]}']); // 无行结束符
  check('incomplete line buffered, nothing dispatched yet', evs.length, 0);
  const rest = p.flush();
  check('flush dispatches what was complete', rest.map((e) => e.data), ['{"choices":[]}']);
  check('flush marks truncated', p.truncated, true);
}
{
  const p = createSseParser();
  feed(p, ['data: x\n']); // 行完整、事件未闭合（缺空行）
  check('flush dispatches pending event', p.flush().map((e) => e.data), ['x']);
  check('truncated because event never closed', p.truncated, true);
}
{
  // 正常收尾：最后一个事件以空行闭合 → flush 不应再派发，也不该标截断
  const p = createSseParser();
  feed(p, ['data: a\n\n', 'data: b\n\n']);
  check('proper close: flush emits nothing', p.flush().length, 0);
  check('proper close: not truncated', p.truncated, false);
}

console.log('防御：');
{
  // 单个事件 data 超限：派发一条 error 事件并丢弃缓冲，不能无限吃内存
  const p = createSseParser({maxDataBytes: 20});
  const evs = feed(p, ['data: 0123456789\ndata: 0123456789\ndata: 0123456789\n\n']);
  check('oversized event surfaces an error event', evs.filter((e) => e.event === 'error').length >= 1, true);
  check('overflow flag set', p.overflowed, true);
}
{
  // BOM 在流首：不能把 BOM 当成字段名的一部分
  const p = createSseParser();
  const evs = feed(p, ['\uFEFFdata: bom\n\n']);
  check('leading BOM tolerated', evs.map((e) => e.data), ['bom']);
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
