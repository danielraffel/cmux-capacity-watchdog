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
read to see the turn running. "write:<path>" answers the next prompt with a
Write tool call for <path>, so Claude shows its permission prompt, then
replies with text once the tool result comes back. "ask" does the same with
an AskUserQuestion call whose second option is marked "(Recommended)". No
model is ever called.
"""

import json
import os
import sys
import threading
import time
import uuid
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
        last = (request.get("messages") or [{}])[-1]
        last_is_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result"
                                  for b in (last.get("content") if isinstance(last.get("content"), list) else []))
        offers_write = any(isinstance(t, dict) and t.get("name") == "Write" for t in request.get("tools") or [])
        offers_ask = any(isinstance(t, dict) and t.get("name") == "AskUserQuestion" for t in request.get("tools") or [])
        if mode.startswith("ask") and offers_ask and not last_is_tool_result and request.get("stream"):
            with lock:
                state["mode"] = "ok"
            return self._tool_use(request, "", mode)
        if mode.startswith("write:") and offers_write and not last_is_tool_result and request.get("stream"):
            # One write per armed mode, so each prompt in a test is distinct.
            with lock:
                state["mode"] = "ok"
            return self._tool_use(request, mode[len("write:"):])
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


ASK_INPUT = {"questions": [{
    "question": "Which stub color should the e2e test use?", "header": "Color", "multiSelect": False,
    "options": [{"label": "Red", "description": "The first option."},
                {"label": "Blue (Recommended)", "description": "The option the watchdog should pick."},
                {"label": "Green", "description": "The third option."}]}]}
# "ask:multi": two single-choice questions and one multi-select.
ASK_MULTI_INPUT = {"questions": ASK_INPUT["questions"] + [
    {"question": "Which stub size should it use?", "header": "Size", "multiSelect": False,
     "options": [{"label": "Small (Recommended)", "description": "Pick me."},
                 {"label": "Large", "description": "Not me."}]},
    {"question": "Which stub extras should it add?", "header": "Extras", "multiSelect": True,
     "options": [{"label": "Logs (Recommended)", "description": "Pick me."},
                 {"label": "Traces", "description": "Not me."}]}]}


def _tool_use_events(model: str, path: str, kind: str = "write") -> list:
    message = {"id": "msg_stub_tool", "type": "message", "role": "assistant", "model": model,
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 1}}
    body = "".join(f"line {i} of a long memory note\n" for i in range(int(os.environ.get("STUB_NOTE_LINES", "1"))))
    tool_input = json.dumps({"file_path": path, "content": "---\nname: e2e-note\ndescription: stub\nmetadata:\n  type: project\n---\n\n" + body})
    tool_name = "Write"
    if kind.startswith("ask"):
        tool_input = json.dumps(ASK_MULTI_INPUT if kind == "ask:multi" else ASK_INPUT)
        tool_name = "AskUserQuestion"
    return [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": f"toolu_stub_{uuid.uuid4().hex[:12]}", "name": tool_name, "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": tool_input}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                           "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _send_tool_use(handler, request: dict, path: str, kind: str = "write") -> None:
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.end_headers()
    for event, data in _tool_use_events(request.get("model", "stub"), path, kind):
        handler.wfile.write(sse(event, data))
    handler.wfile.flush()


Handler._tool_use = lambda self, request, path, kind="write": _send_tool_use(self, request, path, kind)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 31499
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
