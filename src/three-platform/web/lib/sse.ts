/**
 * SSE（Server-Sent Events）分片解析器 —— K31 的协议层实现。
 *
 * 为什么单独抽一个零依赖模块：原来的解析写在 `playground/page.tsx` 的 `send()`
 * 里，20 行按 `\n` 切、`trim()`、遇到 `data:` 就 JSON.parse。它在正常流下能用，
 * 但协议边界全都没覆盖，而且**没法测**（组件里的内联循环）。抽出来以后：
 *   · 浏览器走同一份代码，Node 可以直接 import 做分片/边界测试；
 *   · 组件只负责「拿到事件之后怎么画」，不再自己解释字节流。
 *
 * 按 WHATWG SSE 规范实现（https://html.spec.whatwg.org/multipage/server-sent-events.html），
 * 并做两处**有意的宽容**（都写在这里，避免后人当成 bug 改掉）：
 *
 *  1. 流在事件中途 EOF（没有结尾空行）时，`flush()` 仍把已收齐的 `data` 行派发出来，
 *     同时把 `truncated` 置为 true。规范要求丢弃半截事件——但这里的数据是**已经
 *     完整收到的一行 JSON**，只是少了结束空行；丢弃等于把用户已经看到的半截回答
 *     也一起抹掉。截断标记交给上层的「协议终态」判断（是否收到 `[DONE]`）。
 *  2. 单个分片 JSON 解析失败不抛错（在 chat-stream 层丢弃该事件并计数）。上游偶发
 *     会发非 JSON 的注释/keepalive 行，整条流因此中断反而更糟。
 *
 * 明确**不做**的事：不解释业务 JSON、不判断 `[DONE]`、不管理 reader/AbortController
 * ——那属于 `lib/chat-stream.ts` 的生命周期层。
 */

/** 一个已派发的 SSE 事件。`event` 缺省为 `message`。 */
export interface SseEvent {
  /** 事件类型；规范缺省为 `message` */
  event: string;
  /** 多行 `data:` 以 `\n` 连接（规范要求） */
  data: string;
  /** 最后一个 `id:` 字段；跨事件保留（断线续传用） */
  id?: string;
  /** `retry:` 字段（毫秒）；跨事件保留 */
  retry?: number;
}

export interface SseParserOptions {
  /**
   * 单个事件 `data` 的字节上限（默认 1 MiB）。异常服务端可能无限追加 `data:` 行，
   * 不设上限会把内存打满。超限时派发一个 `event: 'error'` 的合成事件、丢弃缓冲，
   * 并把 `overflowed` 置 true —— 上层据此给出「响应异常」而不是继续拼。
   */
  maxDataBytes?: number;
}

export interface SseParser {
  /** 喂入一个分片（浏览器 `Uint8Array` 或已解码的字符串），返回本次派发的事件 */
  push(chunk: string | Uint8Array): SseEvent[];
  /** 流结束：冲刷解码器尾部字节 + 派发未闭合事件，返回剩余事件 */
  flush(): SseEvent[];
  /** 流结束时是否存在「未闭合事件」（半截关闭） */
  readonly truncated: boolean;
  /** 是否发生过单事件体积超限 */
  readonly overflowed: boolean;
  /** 已派发事件数（诊断用） */
  readonly eventCount: number;
}

const DEFAULT_MAX_DATA_BYTES = 1024 * 1024;

/**
 * 创建一个可增量喂数据的解析器。
 *
 * UTF-8 跨块由 `TextDecoder({stream: true})` 处理：一个中文字符/emoji 被拆在两个
 * 分片里时，第一片解码出空串并保留尾部字节，第二片补齐后一起输出。这是原实现
 * 最隐蔽的缺陷——直接 `decoder.decode(value)`（非 stream）会把半个字符替换成
 * U+FFFD，回答里就出现「���」。
 */
export function createSseParser(options: SseParserOptions = {}): SseParser {
  const maxDataBytes = options.maxDataBytes ?? DEFAULT_MAX_DATA_BYTES;
  const decoder = new TextDecoder('utf-8', {fatal: false, ignoreBOM: false});
  /** 只有超限防护用得到；按真实 UTF-8 字节数算，不能用字符数（CJK 是 3 字节） */
  const encoder = new TextEncoder();

  /** 尚未处理完的文本（可能是半行，或半行 + 可能被拆开的 CRLF） */
  let buf = '';
  /** 当前事件累积的 data 行 */
  let dataLines: string[] = [];
  let dataBytes = 0;
  /** 当前事件类型缓冲（每个事件结束后重置） */
  let eventName = '';
  /** 最后一个事件 id（规范：跨事件保留） */
  let lastId: string | undefined;
  let retry: number | undefined;

  let truncated = false;
  let overflowed = false;
  let eventCount = 0;

  const resetEvent = (): void => {
    dataLines = [];
    dataBytes = 0;
    eventName = '';
  };

  /** 派发当前缓冲的事件；没有 data 行（或 data 为空串）时不派发（规范） */
  const dispatch = (out: SseEvent[]): void => {
    const hasData = dataLines.length > 0;
    const data = dataLines.join('\n');
    if (!hasData || data === '') {
      // `data:\n\n` 这种空事件是合法的 keepalive，但不构成 message
      resetEvent();
      return;
    }
    eventCount += 1;
    const ev: SseEvent = {event: eventName || 'message', data};
    if (lastId !== undefined) ev.id = lastId;
    if (retry !== undefined) ev.retry = retry;
    out.push(ev);
    resetEvent();
  };

  const processLine = (line: string, out: SseEvent[]): void => {
    if (line === '') {
      dispatch(out);
      return;
    }
    if (line.charCodeAt(0) === 0x3a /* ':' */) return; // 注释/心跳

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    // 规范：冒号后**只**去掉一个前导空格
    if (value.charCodeAt(0) === 0x20) value = value.slice(1);

    switch (field) {
      case 'data':
        dataLines.push(value);
        dataBytes += encoder.encode(value).length + 1; // +1 = 行间换行
        if (dataBytes > maxDataBytes) {
          overflowed = true;
          resetEvent();
          out.push({event: 'error', data: `sse event exceeds ${maxDataBytes} bytes`});
        }
        break;
      case 'event':
        eventName = value;
        break;
      case 'id':
        // 规范：含 NUL 的 id 忽略
        if (!value.includes('\u0000')) lastId = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) retry = Number(value);
        break;
      default:
        break; // 未知字段忽略
    }
  };

  /** 从 buf 里切出完整行；`final` 为真时把残留当作最后一行 */
  const consume = (out: SseEvent[], final: boolean): void => {
    let start = 0;
    for (;;) {
      let cr = -1;
      let lf = -1;
      for (let i = start; i < buf.length; i += 1) {
        const c = buf.charCodeAt(i);
        if (c === 0x0a) {
          lf = i;
          break;
        }
        if (c === 0x0d) {
          cr = i;
          break;
        }
      }
      if (lf !== -1) {
        processLine(buf.slice(start, lf), out);
        start = lf + 1;
        continue;
      }
      if (cr !== -1) {
        // CRLF 可能被拆在两个分片里：缓冲末尾的 CR 先等下一个分片确认
        if (cr + 1 === buf.length && !final) break;
        const skip = buf.charCodeAt(cr + 1) === 0x0a ? 2 : 1;
        processLine(buf.slice(start, cr), out);
        start = cr + skip;
        continue;
      }
      break; // 没有行结束符
    }
    buf = buf.slice(start);

    if (final && buf.length > 0) {
      processLine(buf, out);
      buf = '';
    }
  };

  return {
    push(chunk) {
      const out: SseEvent[] = [];
      if (typeof chunk === 'string') {
        // 字符串分片时 TextDecoder 不参与，BOM 要自己剥（字节分片由 decoder 处理）
        const text = buf === '' && chunk.charCodeAt(0) === 0xfeff ? chunk.slice(1) : chunk;
        buf += text;
      } else {
        // stream: true —— 保留不完整的多字节序列到下一次
        buf += decoder.decode(chunk, {stream: true});
      }
      consume(out, false);
      return out;
    },

    flush() {
      const out: SseEvent[] = [];
      // 解码器尾部：把最后一个分片里残缺的多字节序列吐出来（可能是 U+FFFD，
      // 但至少不会把整条流卡住）
      buf += decoder.decode();
      consume(out, true);
      if (dataLines.length > 0) {
        truncated = true;
        dispatch(out);
      }
      return out;
    },

    get truncated() {
      return truncated;
    },
    get overflowed() {
      return overflowed;
    },
    get eventCount() {
      return eventCount;
    },
  };
}
