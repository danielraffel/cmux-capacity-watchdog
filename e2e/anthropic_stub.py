#!/usr/bin/env python3
"""A tiny stand-in for the Anthropic Messages API, for end-to-end tests.

Point a throwaway Claude Code at it (ANTHROPIC_BASE_URL=http://127.0.0.1:PORT)
and drive its mode from the test:

    curl -X POST 'http://127.0.0.1:PORT/_stub/mode?m=fail'   # every request: 503
    curl -X POST 'http://127.0.0.1:PORT/_stub/mode?m=ok'     # slow streamed success
    curl 'http://127.0.0.1:PORT/_stub/stats'                 # request counts

"fail" returns the exact 503 body Subrouter returns when every Claude account
is exhausted, so the client renders the same error a real session shows.
"ok" streams a short reply after OK_DELAY seconds, long enough for a screen
read to see the turn running. No model is ever called.
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FAIL_BODY = {"type": "error", "error": {"type": "api_error",
                                        "message": "no non-exhausted claude accounts available"}}
OK_DELAY = 12.0

state = {"mode": "ok", "requests": 0, "failed": 0, "succeeded": 0}
lock = threading.Lock()


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.command} {self.path} mode={state['mode']}\n")

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/_stub/stats"):
            with lock:
                return self._json(200, dict(state))
        return self._json(200, {"data": [], "has_more": False})

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        if url.path == "/_stub/mode":
            with lock:
                state["mode"] = parse_qs(url.query).get("m", ["ok"])[0]
            return self._json(200, {"mode": state["mode"]})
        if url.path.endswith("/count_tokens"):
            return self._json(200, {"input_tokens": 10})
        try:
            request = json.loads(body or b"{}")
        except json.JSONDecodeError:
            request = {}
        with lock:
            state["requests"] += 1
            mode = state["mode"]
        if mode == "fail":
            with lock:
                state["failed"] += 1
            return self._json(503, FAIL_BODY)
        time.sleep(OK_DELAY)
        with lock:
            state["succeeded"] += 1
        model = request.get("model", "stub")
        message = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 1}}
        text = "Resumed by the stub."
        if not request.get("stream"):
            message.update(content=[{"type": "text", "text": text}], stop_reason="end_turn")
            return self._json(200, message)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(sse("message_start", {"type": "message_start", "message": message}))
        self.wfile.write(sse("content_block_start", {"type": "content_block_start", "index": 0,
                                                     "content_block": {"type": "text", "text": ""}}))
        self.wfile.write(sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                     "delta": {"type": "text_delta", "text": text}}))
        self.wfile.write(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        self.wfile.write(sse("message_delta", {"type": "message_delta",
                                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                               "usage": {"output_tokens": 5}}))
        self.wfile.write(sse("message_stop", {"type": "message_stop"}))
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 31499
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
