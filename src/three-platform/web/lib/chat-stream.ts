/**
 * 流式对话的**生命周期层** —— K31 的另一半。
 *
 * 分工：
 *   `lib/sse.ts`    只解字节流 → SSE 事件（分片、CRLF、多行 data、半截 EOF）；
 *   `lib/chat-stream.ts`（本文件）负责一次请求从发起到收尾的全部资源：
 *     发起 fetch → 判定 HTTP 状态 → 取 reader → 解释事件 → 终态判定 → **释放 reader**；
 *     以及取消（AbortSignal）、错误帧、协议未终止（没有 `[DONE]`）、重复请求防护。
 *
 * 为什么不能继续写在组件里：reader 的释放/取消、aborted 与 truncated 的区别、
 * 「只发起一次请求」这些都属于**可以断言的行为**，放在 `page.tsx` 的 try/catch 里
 * 既测不了也容易被下一次改动冲掉（例如把 `finally` 里的 `cancel()` 删掉不会有任何
 * 视觉变化，只会在断路时把连接拖到超时）。
 *
 * 明确不在这里做的事：不读 localStorage、不带平台头（那属于请求上下文，由页面传入）、
 * 不重试。**一次调用恰好发一次请求**，`requests` 字段供测试断言。
 */
import {createSseParser, type SseEvent} from './sse.ts';
import {channelCheckDetails, type ChannelRejection} from './err-envelope.ts';

/** 实测用量。`credit` 是上游计费口径，缺省即未知，**不能**填 0 冒充。 */
export interface ChatUsage {
  credit: number | null;
  promptTokens: number;
  completionTokens: number;
  /** prompt + completion；上游只给 total_tokens 时用它 */
  tokens: number;
}

export interface ChatStreamResult {
  /** 已收到的正文（取消/截断时保留已收到的部分） */
  answer: string;
  usage: ChatUsage;
  /** 收到 `[DONE]`：协议正常终止 */
  sawDone: boolean;
  /** 没有 `[DONE]` 就结束（半截关闭/上游提前断流），且不是用户取消 */
  truncated: boolean;
  /** 用户主动取消（停止按钮、切平台/路由、卸载） */
  aborted: boolean;
  /** 错误帧或 HTTP 错误的说明；无错误为 null */
  error: string | null;
  channelRejection: ChannelRejection | null;
  httpStatus: number;
  /** JSON 解析失败被丢弃的帧数（诊断用，不影响正文） */
  ignoredEvents: number;
  /** 本次调用实际发起的 HTTP 请求数（正常恒为 1） */
  requests: number;
}

/**
 * 最小化的 fetch 依赖：只要求我们真正用到的字段。
 *
 * 这样测试可以传一个计数假 fetch / 假 reader（`cancel`、`releaseLock` 是否被调用
 * 都能断言），而页面里直接传 `fetch` 也能结构化匹配（`Response` 有更多方法不碍事）。
 */
export interface ReaderLike {
  read(): Promise<{done: boolean; value?: Uint8Array}>;
  cancel?(reason?: unknown): Promise<void>;
  releaseLock?(): void;
}

export interface ResponseLike {
  ok: boolean;
  status: number;
  body: {getReader(): ReaderLike} | null;
  json?(): Promise<unknown>;
  text?(): Promise<string>;
}

export type FetchLike = (url: string, init: RequestInit) => Promise<ResponseLike>;

export interface StreamChatOptions {
  url: string;
  /** 请求体（会被 JSON.stringify） */
  body: unknown;
  headers?: Record<string, string>;
  /** 调用方的取消信号；**必须**传，否则切页/切平台时请求无法取消 */
  signal?: AbortSignal;
  /** 默认用 globalThis.fetch；测试注入假实现 */
  fetchImpl?: FetchLike;
  /** 每收到一段正文回调（增量文本 + 累计文本） */
  onDelta?: (delta: string, full: string) => void;
  /** 收到 usage 时回调 */
  onUsage?: (usage: ChatUsage) => void;
  /** 每个 SSE 事件回调（诊断/测试用，页面不用） */
  onEvent?: (event: SseEvent) => void;
  /** 正文长度上限（默认 2 MiB）：超限按错误处理，避免异常流把内存撑爆 */
  maxAnswerChars?: number;
}

const DEFAULT_MAX_ANSWER_CHARS = 2 * 1024 * 1024;

function emptyUsage(): ChatUsage {
  return {credit: null, promptTokens: 0, completionTokens: 0, tokens: 0};
}

function isAbortError(e: unknown): boolean {
  const err = e as {name?: string; code?: string} | null;
  return !!err && (err.name === 'AbortError' || err.code === 'ABORT_ERR');
}

/** 从非 2xx 响应里取出可展示的错误说明（后端可能返回 detail/error/message 或纯文本） */
async function readErrorDetail(res: ResponseLike, fallback: string): Promise<{message: string; channel: ChannelRejection | null}> {
  let detail = fallback;
  let channel: ChannelRejection | null = null;
  try {
    const data = (await res.json?.()) as Record<string, unknown> | undefined;
    channel = channelCheckDetails(data);
    const pick = (v: unknown): string => (typeof v === 'string' ? v : '');
    const fromObj = (o: Record<string, unknown> | undefined): string => {
      if (!o) return '';
      return (
        pick(o.detail) ||
        pick(o.message) ||
        pick(o.error) ||
        pick((o.error as Record<string, unknown> | undefined)?.message)
      );
    };
    detail = fromObj(data) || detail;
  } catch {
    try {
      const text = (await res.text?.()) || '';
      if (text.trim()) detail = text.trim().slice(0, 500);
    } catch {
      /* 响应体读不出来：保留状态码说明 */
    }
  }
  return {message: detail, channel};
}

/**
 * 发起一次流式对话并读到终态。
 *
 * 资源纪律（每一条都是曾经踩过或容易踩的点）：
 *   · `reader` 在 `finally` 里先 `cancel()`（仅在流**没有自然读完**时）再 `releaseLock()`：
 *     只 release 不 cancel 会让连接停在半路；只 cancel 不 release 会让锁留在
 *     `ReadableStream` 上，同一个 body 再也读不了。
 *   · `aborted` 与 `truncated` 分开：用户点「停止」不是故障，不该显示「回答不完整」；
 *     上游提前断流才是。
 *   · 无论走哪条分支，`requests` 都只可能是 1 —— 重试由页面显式发起。
 */
export async function streamChat(options: StreamChatOptions): Promise<ChatStreamResult> {
  const {
    url,
    body,
    headers,
    signal,
    onDelta,
    onUsage,
    onEvent,
    maxAnswerChars = DEFAULT_MAX_ANSWER_CHARS,
  } = options;
  const fetchImpl = options.fetchImpl ?? (globalThis.fetch as unknown as FetchLike);

  const result: ChatStreamResult = {
    answer: '',
    usage: emptyUsage(),
    sawDone: false,
    truncated: false,
    aborted: false,
    error: null,
    channelRejection: null,
    httpStatus: 0,
    ignoredEvents: 0,
    requests: 0,
  };

  if (signal?.aborted) {
    // 还没发就先取消了：一次请求都不要发
    result.aborted = true;
    return result;
  }

  const parser = createSseParser();
  let reader: ReaderLike | null = null;
  /** 流是否自然读到 EOF（决定 finally 里要不要 cancel） */
  let naturalEnd = false;
  /** 正文超限时主动停止读取（不能靠抛错，否则会打断 finally 之外的清理顺序） */
  let stopReading = false;

  const handleEvent = (ev: SseEvent): void => {
    onEvent?.(ev);
    if (result.sawDone || stopReading) return; // 终态之后的帧一律忽略（有的上游会多发一条 usage）

    if (ev.event === 'error' && !ev.data.trim().startsWith('{')) {
      // 解析器自己合成的错误事件（例如单事件超限）
      result.error = result.error ?? ev.data;
      return;
    }

    const payload = ev.data.trim();
    if (!payload) return;
    if (payload === '[DONE]') {
      result.sawDone = true;
      return;
    }

    let obj: Record<string, unknown> | undefined;
    try {
      obj = JSON.parse(payload) as Record<string, unknown>;
    } catch {
      result.ignoredEvents += 1;
      return;
    }

    // 错误帧：OpenAI 风格 {"error": {...}}；有的网关用 {"error": "text"} 或 {"code","message"}
    const errField = obj?.error;
    if (errField) {
      result.channelRejection ??= channelCheckDetails(obj);
      const e = errField as Record<string, unknown>;
      const msg =
        (typeof errField === 'string' ? errField : '') ||
        (typeof e?.message === 'string' ? e.message : '') ||
        (typeof e?.detail === 'string' ? e.detail : '') ||
        'upstream error';
      const code = e?.code ?? obj?.code;
      result.error = result.error ?? (code != null ? `${msg} (${String(code)})` : msg);
      return;
    }
    if (ev.event === 'error') {
      result.channelRejection ??= channelCheckDetails(obj);
      result.error = result.error ?? (typeof obj?.message === 'string' ? obj.message : payload);
      return;
    }

    const choices = Array.isArray(obj?.choices) ? (obj.choices as Record<string, unknown>[]) : [];
    const first = choices[0] as Record<string, unknown> | undefined;
    const deltaObj = first?.delta as Record<string, unknown> | undefined;
    const msgObj = first?.message as Record<string, unknown> | undefined;
    const delta =
      (typeof deltaObj?.content === 'string' ? (deltaObj.content as string) : '') ||
      (typeof msgObj?.content === 'string' ? (msgObj.content as string) : '') ||
      (typeof first?.text === 'string' ? (first.text as string) : '');
    if (delta) {
      result.answer += delta;
      onDelta?.(delta, result.answer);
      if (result.answer.length > maxAnswerChars) {
        result.error = `response exceeds ${maxAnswerChars} chars`;
        stopReading = true;
      }
    }

    const usage = obj?.usage as Record<string, unknown> | undefined;
    if (usage) {
      const asNum = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
      const prompt = asNum(usage.prompt_tokens);
      const completion = asNum(usage.completion_tokens);
      const total = asNum(usage.total_tokens) || prompt + completion;
      if (typeof usage.credit === 'number' && Number.isFinite(usage.credit)) {
        result.usage.credit = usage.credit;
      }
      result.usage.promptTokens = prompt || result.usage.promptTokens;
      result.usage.completionTokens = completion || result.usage.completionTokens;
      result.usage.tokens = total || result.usage.tokens;
      onUsage?.({...result.usage});
    }
  };

  try {
    let res: ResponseLike;
    result.requests = 1;
    try {
      res = await fetchImpl(url, {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json', ...(headers ?? {})},
        body: JSON.stringify(body),
        signal,
      });
    } catch (e) {
      if (isAbortError(e) || signal?.aborted) {
        result.aborted = true;
        return result;
      }
      result.error = (e as Error)?.message || 'network error';
      return result;
    }

    result.httpStatus = res.status;
    if (!res.ok) {
      const detail = await readErrorDetail(res, `HTTP ${res.status}`);
      result.error = detail.message;
      result.channelRejection = detail.channel;
      return result;
    }

    reader = res.body?.getReader?.() ?? null;
    if (!reader) {
      result.error = 'response body is not readable';
      return result;
    }

    try {
      for (;;) {
        const {done, value} = await reader.read();
        if (done) {
          naturalEnd = true;
          break;
        }
        if (!value || value.length === 0) continue;
        for (const ev of parser.push(value)) handleEvent(ev);
        if (stopReading) break;
      }
    } catch (e) {
      if (isAbortError(e) || signal?.aborted) {
        result.aborted = true;
      } else {
        result.error = result.error ?? ((e as Error)?.message || 'stream read failed');
      }
    }

    // 冲刷解码器尾部与未闭合事件（半截关闭时把已收齐的 JSON 也派发出来）
    for (const ev of parser.flush()) handleEvent(ev);
    if (parser.truncated) result.truncated = true;

    if (!result.aborted && result.error === null && !result.sawDone) {
      // 没有 [DONE] 就结束：协议未终止。可能是上游正常收尾（个别实现不发 DONE），
      // 也可能是断流——上层据此提示「回答可能不完整」，不能静默当成正常完成。
      result.truncated = true;
    }
    return result;
  } finally {
    if (reader) {
      if (!naturalEnd) {
        // 取消/出错/提前 break：显式 cancel，避免半开连接与后台继续计费的流
        try {
          await reader.cancel?.();
        } catch {
          /* 已经关闭/已取消：忽略 */
        }
      }
      try {
        reader.releaseLock?.();
      } catch {
        /* 上游实现可能没有 releaseLock；忽略 */
      }
    }
  }
}
