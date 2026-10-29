"""零第三方依赖的 OpenAI 兼容假上游（Fake Provider）。

设计目标（对应审计包 T02 第 3 条 / 04 矩阵 §7）：
  * 只监听 127.0.0.1，端口默认自动分配；除本机外不可达；
  * 实现 ``GET /v1/models`` 与 ``POST /v1/chat/completions``（流式 + 非流式）；
  * 覆盖：正常成功、延迟首包、4xx/5xx 错误、中途断流、多行 SSE、客户端取消、
    usage 缺失、重复 chunk、超大请求体、超大响应体；
  * 每个请求计数可核对：``GET /__stats``，清零 ``POST /__reset``；
  * 只用标准库（http.server / json / threading / socketserver）。

模式选择优先级：请求体 ``model`` 后缀 ``:mode`` > 请求头 ``X-Fake-Mode`` >
查询串 ``?mode=`` > 启动参数 ``--mode``（默认 ``normal``）。

单独运行::

    python tests/fake_provider/server.py --port 0 --port-file %TEMP%\\fp.json
    python tests/fake_provider/server.py --port 18765 --mode normal --verbose
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# 模式清单：每个模式都有显式语义，测试里按名字引用，禁止“固定 200 假装成功”
# ---------------------------------------------------------------------------
MODES = (
    "normal",           # 正常成功：非流式 JSON / 流式多块 SSE，末帧带 usage
    "delay_first",      # 流式：延迟 first_delay 秒再发首帧（验证首包超时/预算）
    "error_400",        # 400 + OpenAI 风格 error JSON
    "error_429",        # 429 + Retry-After
    "error_500",        # 500
    "error_503",        # 503
    "broken_stream",    # 发 2 帧后直接断开连接，不发 [DONE]（半截关闭）
    "accepted_disconnect",  # 已接收生成请求，响应头之前断开；禁止重放
    "multiline_sse",    # 每帧拆成多行 data:（SSE 多行载荷）
    "crlf_sse",         # SSE 使用 CRLF 行尾 + UTF-8 中文/emoji（跨块解码）
    "cancel",           # 持续发帧直到客户端取消，统计 cancelled
    "missing_usage",    # 成功但 usage 字段缺失
    "duplicate_chunk",  # 同一 delta 帧重复发送两次
    "huge_response",    # 单块超大响应体（默认 8MB）
    "slow_headers",     # 接收请求后延迟 first_delay 秒才返回响应头
)

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def _now() -> float:
    return time.time()


class Stats:
    """线程安全的请求计数器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = _now()
        self.total = 0
        self.by_path: dict[str, int] = {}
        self.by_mode: dict[str, int] = {}
        self.cancelled = 0
        self.completed = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.max_body = 0
        self.errors: list[str] = []
        self.log: list[dict[str, Any]] = []

    def begin(self, path: str, mode: str, body_len: int) -> None:
        with self._lock:
            self.total += 1
            self.by_path[path] = self.by_path.get(path, 0) + 1
            self.by_mode[mode] = self.by_mode.get(mode, 0) + 1
            self.bytes_in += body_len
            self.max_body = max(self.max_body, body_len)
            if len(self.log) < 500:
                self.log.append({"t": round(_now() - self.started_at, 3), "path": path,
                                 "mode": mode, "body": body_len})

    def add_out(self, n: int) -> None:
        with self._lock:
            self.bytes_out += n

    def end(self, cancelled: bool = False) -> None:
        with self._lock:
            if cancelled:
                self.cancelled += 1
            else:
                self.completed += 1

    def note_error(self, msg: str) -> None:
        with self._lock:
            self.errors.append(msg)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self.total,
                "completed": self.completed,
                "cancelled": self.cancelled,
                "by_path": dict(self.by_path),
                "by_mode": dict(self.by_mode),
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
                "max_body": self.max_body,
                "errors": list(self.errors[-20:]),
                "uptime_s": round(_now() - self.started_at, 3),
                "recent": list(self.log[-50:]),
            }

    def reset(self) -> None:
        with self._lock:
            self.started_at = _now()
            self.total = 0
            self.by_path = {}
            self.by_mode = {}
            self.cancelled = 0
            self.completed = 0
            self.bytes_in = 0
            self.bytes_out = 0
            self.max_body = 0
            self.errors = []
            self.log = []


class FakeProvider:
    """可嵌入（in-process）也可独立运行的假上游。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, mode: str = "normal",
                 delay_first: float = 2.0, chunk_delay: float = 0.0,
                 chunk_count: int = 4, huge_bytes: int = 8 * 1024 * 1024,
                 verbose: bool = False) -> None:
        self.stats = Stats()
        self.mode = mode if mode in MODES else "normal"
        self.delay_first = delay_first
        self.chunk_delay = chunk_delay
        self.chunk_count = max(1, chunk_count)
        self.huge_bytes = huge_bytes
        self.verbose = verbose
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._host = host
        self._port = port

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "FakeProvider":
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="fake-provider", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeProvider":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def openai_base_url(self) -> str:
        return f"{self.base_url}/v1"

    # -- handler ---------------------------------------------------------
    def _make_handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "FakeProvider/1.0"

            # --- 通用工具 ---
            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
                if provider.verbose:
                    sys.stderr.write("[fake-provider] %s\n" % (fmt % args))
                    sys.stderr.flush()

            def _send_json(self, code: int, payload: dict[str, Any],
                           extra: dict[str, str] | None = None) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                provider.stats.add_out(len(body))

            def _mode(self, payload: dict[str, Any] | None) -> str:
                mode = ""
                header = self.headers.get("X-Fake-Mode") or ""
                if header:
                    mode = header.strip()
                if not mode:
                    qs = parse_qs(urlparse(self.path).query)
                    mode = (qs.get("mode") or [""])[0]
                if not mode and payload:
                    model = str(payload.get("model") or "")
                    if ":" in model:
                        mode = model.rsplit(":", 1)[1]
                if not mode:
                    mode = provider.mode
                return mode if mode in MODES else "normal"

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return b""
                # 大请求体：分块读，避免一次性 malloc 失败，也统计真实字节
                remaining = length
                buf = bytearray()
                while remaining > 0:
                    piece = self.rfile.read(min(remaining, 256 * 1024))
                    if not piece:
                        break
                    buf.extend(piece)
                    remaining -= len(piece)
                return bytes(buf)

            def _write_chunk(self, data: bytes) -> None:
                """chunked 编码写一块；客户端断开时抛 BrokenPipeError。"""
                self.wfile.write(b"%X\r\n" % len(data))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                provider.stats.add_out(len(data) + 16)

            def _begin_sse(self) -> None:
                self.send_response(200)
                for k, v in SSE_HEADERS.items():
                    self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def _end_chunks(self) -> None:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _sse(self, obj: dict[str, Any] | str, multiline: bool = False,
                     crlf: bool = False) -> bytes:
                if isinstance(obj, str):
                    payload = obj
                else:
                    payload = json.dumps(obj, ensure_ascii=False)
                lines = payload.split("\n") if multiline else [payload]
                nl = "\r\n" if crlf else "\n"
                out = "".join(f"data: {ln}{nl}" for ln in lines) + nl
                return out.encode("utf-8")

            # --- 路由 ---
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path in ("/__stats", "/__stats/"):
                    self._send_json(200, provider.stats.snapshot())
                    return
                if path == "/__health":
                    self._send_json(200, {"ok": True, "mode": provider.mode,
                                          "port": provider.port})
                    return
                if path in ("/v1/models", "/models"):
                    provider.stats.begin(path, "normal", 0)
                    provider.stats.end()
                    self._send_json(200, {
                        "object": "list",
                        "data": [
                            {"id": "fake-model", "object": "model", "created": int(_now()),
                             "owned_by": "fake-provider"},
                            {"id": "fake-model-large", "object": "model",
                             "created": int(_now()), "owned_by": "fake-provider"},
                        ],
                    })
                    return
                self._send_json(404, {"error": {"message": f"no route {path}",
                                                "type": "invalid_request_error"}})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path in ("/__reset", "/__reset/"):
                    provider.stats.reset()
                    self._send_json(200, {"ok": True})
                    return
                raw = self._read_body()
                payload: dict[str, Any] | None = None
                if raw:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except Exception:  # noqa: BLE001 - 非法 JSON 要走 400
                        payload = None
                mode = self._mode(payload)
                provider.stats.begin(path, mode, len(raw))
                if mode == 'accepted_disconnect':
                    provider.stats.end()
                    self.close_connection = True
                    return
                try:
                    if path.endswith("/chat/completions"):
                        self._chat(payload, raw, mode)
                    elif path.endswith("/responses"):
                        self._responses(payload, mode)
                    elif path.endswith("/embeddings"):
                        self._send_json(200, {"object": "list", "data": [
                            {"object": "embedding", "index": 0,
                             "embedding": [0.01, 0.02, 0.03]}],
                            "model": (payload or {}).get("model", "fake-model"),
                            "usage": {"prompt_tokens": 3, "total_tokens": 3}})
                        provider.stats.end()
                    else:
                        self._send_json(404, {"error": {"message": f"no route {path}"}})
                        provider.stats.end()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    provider.stats.end(cancelled=True)
                    self.close_connection = True
                except Exception as exc:  # noqa: BLE001
                    provider.stats.note_error(f"{path}: {exc!r}")
                    provider.stats.end()
                    if provider.verbose:
                        traceback.print_exc()

            # --- 具体实现 ---
            def _stream(self, payload: dict[str, Any] | None, mode: str,
                        include_usage: bool = True) -> None:
                model = str((payload or {}).get("model") or "fake-model")
                multiline = mode == "multiline_sse"
                crlf = mode == "crlf_sse"
                self._begin_sse()
                if mode == "delay_first":
                    time.sleep(provider.delay_first)

                def frames() -> list[dict[str, Any]]:
                    base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                            "created": int(_now()), "model": model}
                    out: list[dict[str, Any]] = []
                    out.append({**base, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                                     "finish_reason": None}]})
                    texts = ["你好，", "这是", "假上游", "的分片。"]
                    for i in range(provider.chunk_count):
                        out.append({**base, "choices": [{
                            "index": 0,
                            "delta": {"content": texts[i % len(texts)]},
                            "finish_reason": None}]})
                    out.append({**base, "choices": [{"index": 0, "delta": {},
                                                     "finish_reason": "stop"}]})
                    if include_usage:
                        out.append({**base, "choices": [],
                                    "usage": {"prompt_tokens": 7, "completion_tokens": 5,
                                              "total_tokens": 12}})
                    return out

                if mode == "broken_stream":
                    # 先送 2 帧（确保客户端真能读到），然后不发终止 chunk 直接关连接
                    for fr in frames()[:2]:
                        self._write_chunk(self._sse(fr, multiline, crlf))
                    self.wfile.flush()
                    time.sleep(0.08)  # 让字节落到对端内核缓冲，形成“半截”
                    provider.stats.end()
                    self.close_connection = True
                    return

                if mode == "cancel":
                    # 模拟“客户端在流中途取消”。Windows 上客户端 close() 后 loopback
                    # 仍可继续写很久（缓冲很大），select 看到 FIN、写超时都不稳定，
                    # 因此做成**确定性**的：发 N 帧（默认 3，可用 X-Fake-Cancel-After
                    # 覆盖）后记录 cancelled 并断开；同时保留写失败/超时判定。
                    import select
                    try:
                        after = int(self.headers.get("X-Fake-Cancel-After") or 3)
                    except ValueError:
                        after = 3
                    after = max(1, min(after, 50))
                    try:
                        self.connection.settimeout(1.5)
                    except OSError:
                        pass
                    for i in range(after):
                        try:
                            ready, _, _ = select.select([self.connection], [], [], 0)
                            if ready:
                                try:
                                    data = self.connection.recv(1, socket.MSG_PEEK)
                                except OSError:
                                    data = b""
                                if not data:
                                    break
                            fr = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                                  "created": int(_now()), "model": model,
                                  "choices": [{"index": 0,
                                               "delta": {"content": f"块{i}；"},
                                               "finish_reason": None}]}
                            self._write_chunk(self._sse(fr, multiline, crlf))
                        except (TimeoutError, socket.timeout, BrokenPipeError,
                                ConnectionResetError, ConnectionAbortedError, OSError):
                            break
                        time.sleep(0.02)
                    provider.stats.end(cancelled=True)
                    self.close_connection = True
                    return

                if mode == "huge_response":
                    blob = "A" * provider.huge_bytes
                    fr = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                          "created": int(_now()), "model": model,
                          "choices": [{"index": 0, "delta": {"content": blob},
                                       "finish_reason": None}]}
                    self._write_chunk(self._sse(fr, multiline, crlf))
                else:
                    for fr in frames():
                        self._write_chunk(self._sse(fr, multiline, crlf))
                        if mode == "duplicate_chunk" and fr.get("choices"):
                            self._write_chunk(self._sse(fr, multiline, crlf))
                        if provider.chunk_delay:
                            time.sleep(provider.chunk_delay)

                self._write_chunk(self._sse("[DONE]", multiline, crlf))
                self._end_chunks()
                provider.stats.end()

            def _chat(self, payload: dict[str, Any] | None, raw: bytes, mode: str) -> None:
                if mode == "slow_headers":
                    time.sleep(provider.delay_first)
                    self._send_json(200, self._completion(payload, with_usage=True))
                    provider.stats.end()
                    return
                if mode == "error_400":
                    self._send_json(400, {"error": {
                        "message": "Invalid 'model': fake bad request",
                        "type": "invalid_request_error", "code": "invalid_model"}})
                    provider.stats.end()
                    return
                if mode == "error_429":
                    self._send_json(429, {"error": {
                        "message": "Rate limit reached for fake provider",
                        "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
                        extra={"Retry-After": "1"})
                    provider.stats.end()
                    return
                if mode == "error_500":
                    self._send_json(500, {"error": {"message": "fake internal error",
                                                    "type": "server_error"}})
                    provider.stats.end()
                    return
                if mode == "error_503":
                    self._send_json(503, {"error": {"message": "fake unavailable",
                                                    "type": "service_unavailable"}})
                    provider.stats.end()
                    return
                stream = bool((payload or {}).get("stream"))
                if stream:
                    include_usage = mode != "missing_usage"
                    self._stream(payload, mode, include_usage=include_usage)
                else:
                    self._send_json(200, self._completion(
                        payload, with_usage=(mode != "missing_usage")))
                    provider.stats.end()

            def _completion(self, payload: dict[str, Any] | None,
                            with_usage: bool) -> dict[str, Any]:
                model = str((payload or {}).get("model") or "fake-model")
                user_text = ""
                for msg in (payload or {}).get("messages") or []:
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        user_text = str(msg.get("content") or "")
                text = f"假上游已收到 {len(user_text)} 个字符的请求。"
                out: dict[str, Any] = {
                    "id": "chatcmpl-fake-1",
                    "object": "chat.completion",
                    "created": int(_now()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }],
                }
                if with_usage:
                    out["usage"] = {"prompt_tokens": 7, "completion_tokens": 5,
                                    "total_tokens": 12}
                return out

            def _responses(self, payload: dict[str, Any] | None, mode: str) -> None:
                """最小 /v1/responses（Responses API）实现，便于协议测试。"""
                model = str((payload or {}).get("model") or "fake-model")
                if mode.startswith("error_"):
                    code = int(mode.split("_")[1])
                    self._send_json(code, {"error": {"message": f"fake {code}"}})
                    provider.stats.end()
                    return
                body = {
                    "id": "resp_fake_1", "object": "response",
                    "created_at": int(_now()), "model": model, "status": "completed",
                    "output": [{"type": "message", "id": "msg_fake_1", "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "假上游响应体",
                                             "annotations": []}]}],
                    "usage": {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
                }
                if mode == "missing_usage":
                    body.pop("usage", None)
                self._send_json(200, body)
                provider.stats.end()

            # 兜底：其它方法
            def do_PUT(self) -> None:  # noqa: N802
                self._send_json(405, {"error": {"message": "method not allowed"}})

        return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OpenAI 兼容假上游（零依赖）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--mode", default="normal", choices=MODES)
    ap.add_argument("--delay-first", type=float, default=2.0)
    ap.add_argument("--chunk-delay", type=float, default=0.0)
    ap.add_argument("--chunk-count", type=int, default=4)
    ap.add_argument("--huge-bytes", type=int, default=8 * 1024 * 1024)
    ap.add_argument("--port-file", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    fp = FakeProvider(host=args.host, port=args.port, mode=args.mode,
                      delay_first=args.delay_first, chunk_delay=args.chunk_delay,
                      chunk_count=args.chunk_count, huge_bytes=args.huge_bytes,
                      verbose=args.verbose).start()
    info = {"base_url": fp.base_url, "openai_base_url": fp.openai_base_url,
            "port": fp.port, "host": args.host, "pid": __import__("os").getpid()}
    if args.port_file:
        with open(args.port_file, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
    sys.stdout.write(json.dumps(info, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        fp.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
