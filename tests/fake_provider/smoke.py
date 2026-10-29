"""假上游冒烟：逐模式验证语义与计数，失败即非零退出。

用法::

    python tests/fake_provider/smoke.py            # 全部模式
    python tests/fake_provider/smoke.py --quick    # 跳过 8MB 大响应/20MB 大请求
    python tests/fake_provider/smoke.py --json     # 机器可读结果

判定原则：每个用例都断言“可观察的协议行为”，不允许只断言 HTTP 200。
"""
from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，强制 UTF-8 以免中文判定信息变成乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_provider.server import FakeProvider  # noqa: E402

RESULTS: list[dict[str, Any]] = []


def _check(name: str, fn) -> None:
    started = time.time()
    try:
        detail = fn()
        RESULTS.append({"name": name, "ok": True,
                        "ms": round((time.time() - started) * 1000, 1),
                        "detail": detail})
    except Exception as exc:  # noqa: BLE001
        RESULTS.append({"name": name, "ok": False,
                        "ms": round((time.time() - started) * 1000, 1),
                        "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(limit=3)})


def _conn(fp: FakeProvider) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", fp.port, timeout=30)


def _post(fp: FakeProvider, path: str, payload: dict[str, Any],
          headers: dict[str, str] | None = None,
          body: bytes | None = None) -> tuple[int, bytes, dict[str, str]]:
    conn = _conn(fp)
    data = body if body is not None else json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    conn.request("POST", path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    out = (resp.status, raw, dict(resp.getheaders()))
    conn.close()
    return out


def _get_json(fp: FakeProvider, path: str) -> dict[str, Any]:
    conn = _conn(fp)
    conn.request("GET", path)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return json.loads(raw.decode())


def _chat(mode: str, stream: bool = True) -> dict[str, Any]:
    return {"model": f"fake-model:{mode}", "stream": stream,
            "messages": [{"role": "user", "content": "你好，假上游"}]}


def _read_sse(fp: FakeProvider, payload: dict[str, Any]) -> tuple[list[str], bytes]:
    conn = _conn(fp)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(payload),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200, f"status={resp.status}"
    frames: list[str] = []
    buf = b""
    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("data:"):
                    frames.append(text[5:].strip())
    finally:
        conn.close()
    return frames, buf


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--delay", type=float, default=1.2)
    args = ap.parse_args(argv)

    fp = FakeProvider(delay_first=args.delay, chunk_delay=0.0, chunk_count=3,
                      huge_bytes=8 * 1024 * 1024).start()
    try:
        def t_models() -> Any:
            data = _get_json(fp, "/v1/models")
            assert data["object"] == "list", data
            ids = [m["id"] for m in data["data"]]
            assert "fake-model" in ids, ids
            return {"models": ids}

        def t_normal_json() -> Any:
            code, raw, _ = _post(fp, "/v1/chat/completions", _chat("normal", False))
            body = json.loads(raw.decode())
            assert code == 200, code
            assert body["choices"][0]["message"]["role"] == "assistant", body
            assert body["usage"]["total_tokens"] == 12, body
            return {"content": body["choices"][0]["message"]["content"]}

        def t_normal_stream() -> Any:
            frames, _ = _read_sse(fp, _chat("normal"))
            assert frames and frames[-1] == "[DONE]", frames[:3]
            assert len(frames) >= 4, len(frames)
            joined = "".join(frames)
            assert "\"role\": \"assistant\"" in joined or '"role": "assistant"' in joined, joined[:200]
            return {"frames": len(frames)}

        def t_delay_first() -> Any:
            started = time.time()
            frames, _ = _read_sse(fp, _chat("delay_first"))
            elapsed = time.time() - started
            assert elapsed >= args.delay * 0.8, f"首包仅 {elapsed:.2f}s"
            assert frames[-1] == "[DONE]", frames[-1:]
            return {"elapsed_s": round(elapsed, 3)}

        def t_errors() -> Any:
            seen = {}
            for mode, expect in (("error_400", 400), ("error_429", 429),
                                 ("error_500", 500), ("error_503", 503)):
                code, raw, _ = _post(fp, "/v1/chat/completions", _chat(mode, False))
                assert code == expect, (mode, code)
                assert b"error" in raw, (mode, raw[:120])
                seen[mode] = code
            return seen

        def t_broken_stream() -> Any:
            conn = _conn(fp)
            conn.request("POST", "/v1/chat/completions",
                         body=json.dumps(_chat("broken_stream")),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            buf = b""
            broken = False
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > 65536:
                        break
            except http.client.IncompleteRead as exc:
                buf += exc.partial
                broken = True
            except (ConnectionError, socket.timeout):
                broken = True
            finally:
                conn.close()
            assert b"[DONE]" not in buf, "断流模式不应出现 [DONE]"
            assert len(buf) > 0, "客户端未读到任何半截数据（不是真正的断流）"
            return {"bytes_before_break": len(buf), "incomplete_read": broken}

        def t_multiline_sse() -> Any:
            frames, raw = _read_sse(fp, _chat("multiline_sse"))
            # 多行 data 会被客户端拼成一行一行，但服务端发出的是多行帧
            assert any("\n" in f for f in frames) or len(frames) > 3, frames[:5]
            assert frames[-1].endswith("[DONE]") or "[DONE]" in frames[-1], frames[-1:]
            return {"frames": len(frames)}

        def t_crlf_sse() -> Any:
            frames, _ = _read_sse(fp, _chat("crlf_sse"))
            assert any("假上游" in f or "你好" in f for f in frames), frames[:5]
            assert "[DONE]" in frames[-1], frames[-1:]
            return {"frames": len(frames)}

        def t_missing_usage() -> Any:
            code, raw, _ = _post(fp, "/v1/chat/completions", _chat("missing_usage", False))
            body = json.loads(raw.decode())
            assert code == 200 and "usage" not in body, body
            return {"has_usage": False}

        def t_duplicate_chunk() -> Any:
            frames, _ = _read_sse(fp, _chat("duplicate_chunk"))
            deltas = [f for f in frames if '"delta"' in f and '"content"' in f]
            assert len(deltas) >= 2, len(deltas)
            # 相邻两帧完全相同 => 重复 chunk 生效
            dup = any(deltas[i] == deltas[i + 1] for i in range(len(deltas) - 1))
            assert dup, "未观察到重复 chunk"
            return {"delta_frames": len(deltas), "duplicate_adjacent": dup}

        def t_cancel() -> Any:
            before = _get_json(fp, "/__stats")
            conn = _conn(fp)
            conn.request("POST", "/v1/chat/completions",
                         body=json.dumps(_chat("cancel")),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            first = resp.read(1)
            assert first, "未收到首字节"
            time.sleep(0.3)
            conn.close()
            deadline = time.time() + 5
            while time.time() < deadline:
                after = _get_json(fp, "/__stats")
                if after["cancelled"] > before["cancelled"]:
                    return {"cancelled_delta": after["cancelled"] - before["cancelled"]}
                time.sleep(0.1)
            raise AssertionError(f"取消未计数：{_get_json(fp, '/__stats')}")

        def t_huge_request() -> Any:
            big = b"x" * (20 * 1024 * 1024)
            payload = {"model": "fake-model:normal", "stream": False,
                       "messages": [{"role": "user", "content": big.decode()}]}
            body = json.dumps(payload).encode()
            code, raw, _ = _post(fp, "/v1/chat/completions", {}, body=body)
            assert code == 200, code
            stats = _get_json(fp, "/__stats")
            assert stats["max_body"] >= 20 * 1024 * 1024, stats["max_body"]
            return {"request_bytes": len(body), "max_body": stats["max_body"]}

        def t_huge_response() -> Any:
            conn = _conn(fp)
            conn.request("POST", "/v1/chat/completions",
                         body=json.dumps(_chat("huge_response")),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            total = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
            conn.close()
            assert total >= 8 * 1024 * 1024, total
            return {"bytes": total}

        def t_stats_counted() -> Any:
            stats = _get_json(fp, "/__stats")
            assert stats["total"] >= 10, stats
            assert stats["by_path"].get("/v1/chat/completions", 0) >= 8, stats["by_path"]
            assert stats["cancelled"] >= 1, stats
            return {"total": stats["total"], "by_mode": stats["by_mode"],
                    "cancelled": stats["cancelled"]}

        _check("GET /v1/models 返回模型列表", t_models)
        _check("normal 非流式：200 + usage", t_normal_json)
        _check("normal 流式：多帧 + [DONE]", t_normal_stream)
        _check("delay_first：首包延迟符合配置", t_delay_first)
        _check("error_400/429/500/503：状态码与错误体", t_errors)
        _check("broken_stream：无 [DONE] 的半截关闭", t_broken_stream)
        _check("multiline_sse：多行 data 帧", t_multiline_sse)
        _check("crlf_sse：CRLF + UTF-8 中文", t_crlf_sse)
        _check("missing_usage：成功但无 usage", t_missing_usage)
        _check("duplicate_chunk：重复 delta 帧", t_duplicate_chunk)
        _check("cancel：客户端取消被计数", t_cancel)
        if not args.quick:
            _check("超大请求体 20MB 可接收并计量", t_huge_request)
            _check("超大响应体 8MB 可完整读出", t_huge_response)
        _check("/__stats 总计数与分模式计数", t_stats_counted)
    finally:
        fp.stop()

    passed = sum(1 for r in RESULTS if r["ok"])
    failed = len(RESULTS) - passed
    if args.json:
        print(json.dumps({"passed": passed, "failed": failed, "results": RESULTS},
                         ensure_ascii=False, indent=2))
    else:
        for r in RESULTS:
            mark = "PASS" if r["ok"] else "FAIL"
            extra = r.get("detail") if r["ok"] else r.get("error")
            print(f"[{mark}] {r['name']}  ({r['ms']}ms)  {extra}")
    print(f"\n假上游冒烟：{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
