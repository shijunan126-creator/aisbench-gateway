#!/usr/bin/env python3
"""一个最小的 OpenAI 兼容假模型服务，用于在没有真实模型服务时端到端自测网关。

    python3 tools/mock_openai_server.py --port 8000

支持 /v1/completions 与 /v1/chat/completions 的流式与非流式，
并模拟可配置的 TTFT 与每 token 生成耗时，这样压测出来的
吞吐/时延数字是可以交叉验算的：

    并发 C、输出 N 个 token、每 token 耗时 t、TTFT d 时
    单请求 E2EL ≈ d + N*t
    输出吞吐 ≈ C*N / (d + N*t)

**只用 Python 标准库**（http.server），和网关保持一致 —— 因为它跑在
aisbench 容器里，而那个容器里没有 fastapi/uvicorn。

仅用于自测，不要拿去当生产服务。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn

CFG = {
    "ttft_ms": 50.0,    # 首 token 延迟
    "tpot_ms": 5.0,     # 每 token 生成耗时
    "max_tokens": 512,  # 单请求输出上限
}

VOCAB = ["的", "是", "在", "和", "有", "模型", "推理", "性能", "测试", "数据",
         "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "and",
         "benchmark", "throughput", "latency", "token", "sequence"]


def _make_text(n: int) -> str:
    return "".join(VOCAB[i % len(VOCAB)] for i in range(n))


# ---------------------------------------------------------------- prefix cache 模拟
#
# 网关的「采集 prefix cache 命中率」要读 vLLM 的 /metrics。假服务要是没有这个
# 端点，那条链路就永远测不到（只会走到"取数失败"的分支），所以这里补一个
# **粗略**的模拟：
#
#   把 prompt 按固定字符数切成块，维护一个"见过的块"集合；
#   一条新请求命中的，是它从开头起连续出现过的那几个块。
#
# 这对应真实 prefix cache 的语义（前缀一致才命中，一旦分叉后面全不命中）。
# 这里没有 tokenizer，所以按字符切 —— **数值不代表任何真实引擎的行为**，
# 只用来验证网关的采集、增量计算和展示是否正确。

BLOCK_CHARS = 16
CACHE_CAP = 200_000
ENGINES = 2          # 模拟 2 个 DP 域，顺带验证网关的分 engine 统计

_LOCK = threading.Lock()
_SEEN = [set() for _ in range(ENGINES)]
_COUNTERS = [{"queries": 0.0, "hits": 0.0} for _ in range(ENGINES)]
_RR = {"next": 0}


def _extract_prompt(body: dict, chat: bool) -> str:
    if chat:
        parts = []
        for m in body.get("messages") or []:
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):        # content 是数组的多模态写法
                parts.extend(p.get("text", "") for p in c if isinstance(p, dict))
        return "\n".join(parts)
    p = body.get("prompt")
    if isinstance(p, list):
        return "\n".join(str(x) for x in p)
    return str(p or "")


def _record_prefix_cache(body: dict, chat: bool) -> None:
    prompt = _extract_prompt(body, chat)
    blocks = [prompt[i:i + BLOCK_CHARS] for i in range(0, len(prompt), BLOCK_CHARS)]
    if not blocks:
        return
    with _LOCK:
        # 轮流分给两个 engine，这样两边都能吃到请求，分域统计才有东西可看
        eid = _RR["next"] % ENGINES
        _RR["next"] += 1
        seen = _SEEN[eid]
        hits = 0
        for b in blocks:
            if b in seen:
                hits += 1
            else:
                break                        # 前缀一旦分叉，后面都不算命中
        _COUNTERS[eid]["queries"] += len(blocks)
        _COUNTERS[eid]["hits"] += hits
        seen.update(blocks)
        if len(seen) > CACHE_CAP:
            seen.clear()


def _metrics_text() -> str:
    with _LOCK:
        counters = [dict(c) for c in _COUNTERS]
    out = ["# HELP vllm:prefix_cache_queries_total Number of prefix cache queries.",
           "# TYPE vllm:prefix_cache_queries_total counter"]
    for i, c in enumerate(counters):
        out.append(f'vllm:prefix_cache_queries_total'
                   f'{{model_name="mock-model",engine="{i}"}} {c["queries"]:.0f}')
    out.append("# HELP vllm:prefix_cache_hits_total Number of prefix cache hits.")
    out.append("# TYPE vllm:prefix_cache_hits_total counter")
    for i, c in enumerate(counters):
        out.append(f'vllm:prefix_cache_hits_total'
                   f'{{model_name="mock-model",engine="{i}"}} {c["hits"]:.0f}')
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音，避免刷屏
        pass

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list",
                             "data": [{"id": "mock-model", "object": "model"}]})
        elif self.path.rstrip("/") == "/metrics":
            # Prometheus 文本格式。用它验证网关的 prefix cache 命中率采集
            self._text(200, _metrics_text())
        elif self.path in ("/", "/health"):
            self._json(200, {"ok": True, "service": "mock-openai"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            body = self._read_json()
        except ValueError as e:
            self._json(400, {"error": {"message": str(e)}})
            return

        if path == "/v1/completions":
            self._respond(body, chat=False)
        elif path == "/v1/chat/completions":
            self._respond(body, chat=True)
        else:
            self._json(404, {"error": "not found"})

    # ------------------------------------------------------------ 辅助
    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"请求体不是合法 JSON: {e}") from e

    def _json(self, status, obj):
        self._text(status, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _text(self, status, text, ctype="text/plain; charset=utf-8"):
        data = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ 主逻辑
    def _respond(self, body, chat):
        _record_prefix_cache(body, chat)
        stream = bool(body.get("stream"))
        n = min(int(body.get("max_tokens") or 64), CFG["max_tokens"])
        rid = f"cmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())
        model = body.get("model") or "mock-model"

        if not stream:
            time.sleep(CFG["ttft_ms"] / 1000 + n * CFG["tpot_ms"] / 1000)
            text = _make_text(n)
            if chat:
                choice = {"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": text}}
                obj = "chat.completion"
            else:
                choice = {"index": 0, "finish_reason": "stop", "text": text}
                obj = "text_completion"
            self._json(200, {
                "id": rid, "object": obj, "created": created, "model": model,
                "choices": [choice],
                "usage": {"prompt_tokens": 16, "completion_tokens": n,
                          "total_tokens": 16 + n},
            })
            return

        # 流式：HTTP/1.1 下没有 Content-Length 就必须靠关闭连接来定界，
        # 所以显式声明 Connection: close 并在结束时关闭。
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            time.sleep(CFG["ttft_ms"] / 1000)
            for i in range(n):
                if chat:
                    delta = {"role": "assistant"} if i == 0 else {}
                    delta["content"] = VOCAB[i % len(VOCAB)]
                    chunk = {"id": rid, "object": "chat.completion.chunk",
                             "created": created, "model": model,
                             "choices": [{"index": 0, "delta": delta,
                                          "finish_reason": None}]}
                else:
                    chunk = {"id": rid, "object": "text_completion",
                             "created": created, "model": model,
                             "choices": [{"index": 0, "text": VOCAB[i % len(VOCAB)],
                                          "finish_reason": None}]}
                send(chunk)
                time.sleep(CFG["tpot_ms"] / 1000)

            done = {"id": rid,
                    "object": "chat.completion.chunk" if chat else "text_completion",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "text": "",
                                 "finish_reason": "stop"}]}
            send(done)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--ttft-ms", type=float, default=50.0)
    ap.add_argument("--tpot-ms", type=float, default=5.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    a = ap.parse_args()

    CFG["ttft_ms"] = a.ttft_ms
    CFG["tpot_ms"] = a.tpot_ms
    CFG["max_tokens"] = a.max_tokens

    srv = Server((a.host, a.port), Handler)
    print(f"假模型服务已启动 http://{a.host}:{a.port}  "
          f"TTFT={a.ttft_ms}ms TPOT={a.tpot_ms}ms", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
