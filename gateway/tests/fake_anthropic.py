"""A fake of the two model APIs, over real HTTP, so the real SDKs talk to it.

The Messages API part refuses what the real one refuses on today's models, with the same
status and words (the SDK's documentation is the source, nothing here was seen on the real
API): a forced `tool_choice`, sampling settings, `thinking` disabled or with a budget, a
missing `max_tokens`, roles that do not alternate, a tool result without its call, a thinking
block replayed after the conversation before it changed. It records every request, and
answers with a script: tool calls, thinking, refusals, errors, in one piece or streamed.

Also a small OpenAI-compatible endpoint, for a conversation that moves between the two.

    python tests/fake_anthropic.py 8765     # standing alone, for a try in the browser (see `_LIVE`)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

API_KEY = "sk-ant-fake-key"
FORCED_400 = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1")
NO_BUDGET = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-5", "claude-opus-4-7", "claude-opus-4-8")
NO_DISABLE = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5")
NO_SAMPLING = ("claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-5")
NO_EFFORT = ("claude-haiku-4-5", "claude-sonnet-4-5")
KNOWN_BETAS = ("thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01", "server-side-fallback-2026-06-01")
DEFAULT_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-haiku-4-5"]
_TOOL_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def text(words: str, **rest: Any) -> dict:
    return {"content": [{"type": "text", "text": words}], "stop_reason": "end_turn", **rest}


def tools(*calls: tuple[str, str, dict], words: str = "", thinking: bool = True, **rest: Any) -> dict:
    """A turn of tool calls: (id, name, input). In parallel when more than one."""
    content: list[dict] = [{"type": "thinking"}] if thinking else []
    if words:
        content.append({"type": "text", "text": words})
    content += [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls]
    return {"content": content, "stop_reason": "tool_use", **rest}


def thought(words: str, **rest: Any) -> dict:
    """A thinking block, its text omitted as it is by default on today's models, then the text."""
    return {"content": [{"type": "thinking"}, {"type": "text", "text": words}], "stop_reason": "end_turn", **rest}


def refusal(partial: str = "", category: str = "cyber") -> dict:
    content = [{"type": "text", "text": partial}] if partial else []
    return {"content": content, "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": category, "explanation": None}}


def failure(status: int, kind: str, message: str = "failed", headers: Optional[dict] = None) -> dict:
    return {"status": status, "error": {"type": kind, "message": message}, "headers": headers or {}}


OVERLOADED = failure(529, "overloaded_error", "Overloaded", {"retry-after": "0"})
RATE_LIMITED = failure(429, "rate_limit_error", "Too many requests", {"retry-after": "0"})


def _no_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _no_markers(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_no_markers(v) for v in value]
    return value


def _binding(body: dict, messages: list[dict]) -> str:
    """What a thinking block's signature holds: the system prompt, the set of tools and every
    message before it, earlier thinking blocks aside. Markers of the cache do not count."""
    wanted = {
        "system": _no_markers(body.get("system")),
        "tools": sorted((_no_markers(t) for t in body.get("tools") or []), key=lambda t: t.get("name", "")),
        "messages": [{**_no_markers(m), "content": [b for b in _no_markers(m["content"]) if b.get("type") != "thinking"]
                      if isinstance(m["content"], list) else m["content"]} for m in messages],
    }
    return hashlib.sha256(json.dumps(wanted, sort_keys=True).encode()).hexdigest()[:24]


def _signature(model: str, body: dict, messages: list[dict]) -> str:
    return f"sig:{model}:{_binding(body, messages)}"


class FakeProvider:
    def __init__(self, models: Optional[list[str]] = None, key: str = API_KEY, live: bool = False):
        self.models = list(models if models is not None else DEFAULT_MODELS)
        self.key = key
        self.live = live
        self.script: list[dict] = []
        self.requests: list[dict] = []  # the POST /v1/messages that reached the model's rules, in order
        self.attempts: list[dict] = []  # every request, including those refused
        self.catalog_error: Optional[dict] = None
        self.chat_requests: list[dict] = []  # the OpenAI-compatible endpoint's
        self.chat_script: list[dict] = []
        self._server: Optional[ThreadingHTTPServer] = None

    # ── the life of the server ──
    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self, port: int = 0) -> "FakeProvider":
        provider = self

        class Handler(_Handler):
            fake = provider

        self._server = ThreadingHTTPServer(("0.0.0.0" if port else "127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ── the rules ──
    def _refuse(self, body: dict, headers: dict) -> Optional[dict]:
        model = str(body.get("model") or "")
        wrong = lambda message: failure(400, "invalid_request_error", message)  # noqa: E731
        for field in ("model", "max_tokens", "messages"):
            if field not in body:
                return wrong(f"{field}: Field required")
        if not body["messages"]:
            return wrong("messages: at least one message is required")
        if model not in self.models:
            return failure(404, "not_found_error", f"model: {model}")
        betas = [b.strip() for b in (headers.get("anthropic-beta") or "").split(",") if b.strip()]
        for beta in betas:
            if beta not in KNOWN_BETAS:
                return wrong(f"Unexpected value(s) `{beta}` for the `anthropic-beta` header.")
        if model.startswith(NO_SAMPLING):
            for field in ("temperature", "top_p", "top_k"):
                if field in body:
                    return wrong(f"{field}: is not supported on this model")
        choice = (body.get("tool_choice") or {}).get("type")
        if choice in ("any", "tool") and model.startswith(FORCED_400):
            return wrong('tool_choice: type "tool" and "any" are not supported for this model.')
        thinking = body.get("thinking") or {}
        if thinking.get("type") == "disabled" and model.startswith(NO_DISABLE):
            return wrong('"thinking.type.disabled" is not supported for this model.')
        if thinking.get("type") == "enabled" and model.startswith(NO_BUDGET):
            return wrong('"thinking.type.enabled" is not supported for this model.')
        if "block_binding" in thinking and "thinking-binding-controls-2026-08-01" not in betas:
            return wrong("block_binding: Extra inputs are not permitted")
        if (body.get("output_config") or {}).get("effort") and model.startswith(NO_EFFORT):
            return wrong("output_config.effort: is not supported on this model")
        for tool in body.get("tools") or []:
            if not _TOOL_NAME.match(str(tool.get("name"))):
                return wrong(f"tools: name {tool.get('name')!r} does not match the pattern")
            if any(k in (tool.get("input_schema") or {}) for k in ("oneOf", "allOf", "anyOf")):
                return wrong("tools: input_schema does not support oneOf, allOf, or anyOf at the top level")
        schema = ((body.get("output_config") or {}).get("format") or {}).get("schema")
        if schema is not None and _open_object(schema):
            return wrong("output_config.format.schema: For 'object' type, 'additionalProperties' must be explicitly set to false")
        return self._refuse_messages(body, model, thinking)

    def _refuse_messages(self, body: dict, model: str, thinking: dict) -> Optional[dict]:
        wrong = lambda message: failure(400, "invalid_request_error", message)  # noqa: E731
        messages = body["messages"]
        if messages[0]["role"] != "user":
            return wrong("messages: first message must use the user role")
        waiting: set[str] = set()
        dropping = False
        for i, message in enumerate(messages):
            content = message["content"] if isinstance(message["content"], list) else [{"type": "text", "text": message["content"]}]
            if i and messages[i - 1]["role"] == message["role"]:
                return wrong('messages: roles must alternate between "user" and "assistant"')
            if message["role"] == "user":
                results = {b["tool_use_id"] for b in content if b.get("type") == "tool_result"}
                if waiting - results:
                    return wrong(f"messages.{i}: tool_use ids were found without tool_result blocks immediately after")
                if results - waiting:
                    return wrong(f"messages.{i}.content: unexpected tool_use_id found in tool_result blocks")
                waiting = set()
                continue
            for j, block in enumerate(content):
                if block.get("type") == "tool_use":
                    if not _TOOL_ID.match(str(block.get("id"))):
                        return wrong(f"messages.{i}.content.{j}.tool_use.id: String should match pattern")
                    waiting.add(block["id"])
                if block.get("type") == "text" and not block.get("text"):
                    return wrong(f"messages.{i}.content.{j}: text content blocks must be non-empty")
                if block.get("type") != "thinking":
                    continue
                signature = str(block.get("signature") or "")
                parts = signature.split(":")
                if len(parts) != 3 or parts[0] != "sig":
                    return wrong(f"messages.{i}.content.{j}: Invalid `signature` in `thinking` block.")
                if parts[1] != model:  # another model's block: dropped, not refused
                    continue
                if dropping or parts[2] != _binding(body, messages[:i]):
                    if thinking.get("block_binding", {}).get("prefix_mismatch_behavior") == "drop_block":
                        dropping = True
                        body.setdefault("_dropped", []).append(f"messages.{i}.content.{j}")
                        continue
                    return wrong(f"messages.{i}.content.{j}: Invalid `signature` in `thinking` block. The block is bound to a "
                                 "different conversation. Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` "
                                 'to "drop_block". That setting requires the `thinking-binding-controls-2026-08-01` value in '
                                 "the `anthropic-beta` header.")
            if not any(b.get("type") != "thinking" for b in content) and i != len(messages) - 1:
                return wrong(f"messages.{i}: the message must contain something besides thinking")
        return wrong(f"messages.{len(messages)}: tool_use ids were found without tool_result blocks immediately after") if waiting else None

    def _answer(self, body: dict) -> dict:
        if self.live:
            return self._live_answer(body)
        return self.script.pop(0) if self.script else text("ok")

    def _live_answer(self, body: dict) -> dict:
        """Standing alone, what the question asks for (`_LIVE`)."""
        last = body["messages"][-1]
        content = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": last["content"]}]
        asked = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        if any(b.get("type") == "tool_result" for b in content):
            return thought("The fake provider read the tool's results, and answers with them.")
        if "REFUSE" in asked:
            return refusal()
        if "OVERLOADED" in asked:
            return OVERLOADED
        if "TOOLS" in asked and any(t.get("name") == "list_datasources" for t in body.get("tools") or []):
            return tools(("toolu_live_a", "list_datasources", {}), ("toolu_live_b", "list_datasources", {"search": "orders"}),
                         words="Looking at the catalog.")
        return thought(f"Answer from the fake provider to: {asked[:80]}", usage={"input_tokens": 1000, "output_tokens": 50})


def _open_object(schema: Any) -> bool:
    if isinstance(schema, dict):
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            return True
        return any(_open_object(v) for v in schema.values())
    if isinstance(schema, list):
        return any(_open_object(v) for v in schema)
    return False


class _Handler(BaseHTTPRequestHandler):
    fake: FakeProvider
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: Any, headers: Optional[dict] = None) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, reply: dict) -> None:
        self._json(reply["status"], {"type": "error", "error": reply["error"], "request_id": "req_fake"}, reply.get("headers"))

    def _authorized(self) -> bool:
        if self.headers.get("x-api-key") == self.fake.key or self.headers.get("authorization") == f"Bearer {self.fake.key}":
            return True
        self._error(failure(401, "authentication_error", "invalid x-api-key"))
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/_requests":
            return self._json(200, {"messages": self.fake.requests, "chat": self.fake.chat_requests})
        if not self._authorized():
            return None
        if self.fake.catalog_error is not None:
            return self._error(self.fake.catalog_error)
        if path == "/v1/models":
            ids = self.fake.models
            if "anthropic-version" in {k.lower() for k in self.headers.keys()}:
                # two pages, as the real one can answer: the SDK goes through them
                after = next((p.split("=")[1] for p in self.path.split("?")[-1].split("&") if p.startswith("after_id=")), None)
                start = ids.index(after) + 1 if after in ids else 0
                page = ids[start:start + 2]
                return self._json(200, {"data": [{"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z"} for m in page],
                                        "has_more": start + 2 < len(ids), "first_id": page[0] if page else None, "last_id": page[-1] if page else None})
            return self._json(200, {"data": [{"id": m, "object": "model"} for m in ids]})
        return self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path = self.path.split("?")[0]
        if not self._authorized():
            return None
        if path == "/v1/chat/completions":
            return self._chat_completion(body)
        if path != "/v1/messages":
            return self._json(404, {"detail": "not found"})
        headers = {k.lower(): v for k, v in self.headers.items()}
        record = {"body": json.loads(json.dumps(body)), "headers": {k: v for k, v in headers.items() if k not in ("x-api-key", "authorization")}}
        self.fake.attempts.append(record)
        refused = self.fake._refuse(body, headers)
        if refused is not None:
            record["refused"] = refused["error"]["message"]
            return self._error(refused)
        self.fake.requests.append(record)
        reply = self.fake._answer(body)
        if "status" in reply:
            return self._error(reply)
        message = self._message(body, reply)
        if body.get("stream"):
            return self._stream(message, reply)
        return self._json(200, message)

    def _message(self, body: dict, reply: dict) -> dict:
        model = body["model"]
        content = []
        for block in reply["content"]:
            if block["type"] == "thinking":
                # the text is omitted, the signature holds everything sent before it
                content.append({"type": "thinking", "thinking": "", "signature": _signature(model, body, body["messages"])})
            else:
                content.append(block)
        usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                 **reply.get("usage", {})}
        message: dict[str, Any] = {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": reply.get("served_by", model), "content": content,
            "stop_reason": reply.get("stop_reason"), "stop_sequence": None, "stop_details": reply.get("stop_details"), "usage": usage,
        }
        if body.get("_dropped"):
            message["input_transformations"] = [{"type": "thinking_dropped", "path": p, "reason": "prefix_binding_mismatch"}
                                                for p in body["_dropped"]]
        return message

    def _send_event(self, kind: str, payload: dict) -> None:
        raw = f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n".encode()
        self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
        self.wfile.flush()

    def _stream(self, message: dict, reply: dict) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        usage = message["usage"]
        start = {**message, "content": [], "stop_reason": None, "stop_details": None, "usage": {**usage, "output_tokens": 1}}
        self._send_event("message_start", {"message": start})
        for i, block in enumerate(message["content"]):
            kind = block["type"]
            if kind == "text":
                self._send_event("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}})
                self._send_event("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": block["text"]}})
            elif kind == "thinking":
                self._send_event("content_block_start", {"index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                self._send_event("content_block_delta", {"index": i, "delta": {"type": "signature_delta", "signature": block["signature"]}})
            elif kind == "tool_use":
                self._send_event("content_block_start", {"index": i, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
                # `cut`: the arguments stop halfway, as when `max_tokens` runs out
                sent = json.dumps(block["input"])
                self._send_event("content_block_delta", {"index": i, "delta": {"type": "input_json_delta", "partial_json": sent[:len(sent) // 2] if block.get("cut") else sent}})
            else:  # a block the client may not know
                self._send_event("content_block_start", {"index": i, "content_block": block})
            self._send_event("content_block_stop", {"index": i})
            time.sleep(0.001)
        if reply.get("stream_error"):
            self._send_event("error", {"error": reply["stream_error"], "request_id": "req_fake"})
        else:
            self._send_event("message_delta", {
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None, "stop_details": message["stop_details"]},
                "usage": {"output_tokens": usage["output_tokens"]},
            })
            self._send_event("message_stop", {})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _chat_completion(self, body: dict) -> None:
        self.fake.chat_requests.append(json.loads(json.dumps(body)))
        reply = self.fake.chat_script.pop(0) if self.fake.chat_script else {"content": "ok from the other provider"}
        chunk = lambda delta, finish=None: "data: " + json.dumps({  # noqa: E731
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": body.get("model"),
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
        if reply.get("tool_calls"):
            calls = [{"index": i, "id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                     for i, c in enumerate(reply["tool_calls"])]
            stream = chunk({"role": "assistant", "tool_calls": calls}) + chunk({}, "tool_calls")
        else:
            stream = chunk({"role": "assistant", "content": reply["content"]}) + chunk({}, "stop")
        raw = (stream + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


# Standing alone: REFUSE in a question declines it, OVERLOADED answers 529, TOOLS calls
# list_datasources twice in parallel. GET /_requests shows what it received.
_LIVE = __doc__

if __name__ == "__main__":
    server = FakeProvider(live=True).start(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"fake provider on {server.url}", flush=True)
    while True:
        time.sleep(3600)
