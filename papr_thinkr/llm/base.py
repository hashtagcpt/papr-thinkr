"""The interface every LLM backend implements (BaseLLM: chat() and
chat_json()), the shared errors, the per-job call context (cancellation and
usage accounting), and helpers for getting structured JSON out of models."""
from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 1800


_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,120}$")


class LLMError(RuntimeError):
    pass


class LLMCancelled(LLMError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status: int, body: str, url: str = ""):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status} from {url}: {body[:600]}")


class CallContext:
    """Collects usage stats for one job and carries its cancel flag. Install it
    in every thread that makes LLM calls on the job's behalf (use_context)."""

    def __init__(self, cancel_event: threading.Event | None = None):
        self.cancel_event = cancel_event
        self.calls = 0
        self.failures = 0
        self.chars_in = 0
        self.chars_out = 0
        self.seconds = 0.0
        self._lock = threading.Lock()

    def record(self, chars_in: int, chars_out: int, seconds: float, ok: bool = True) -> None:
        with self._lock:
            self.calls += 1
            self.failures += 0 if ok else 1
            self.chars_in += chars_in
            self.chars_out += chars_out
            self.seconds += seconds

    def as_dict(self) -> dict:
        return {"calls": self.calls, "failures": self.failures, "chars_in": self.chars_in,
                "chars_out": self.chars_out, "model_seconds": round(self.seconds, 1)}


_local = threading.local()


def use_context(ctx: CallContext | None) -> None:
    _local.ctx = ctx


def current_context() -> CallContext | None:
    return getattr(_local, "ctx", None)


def is_cancelled() -> bool:
    ctx = current_context()
    return bool(ctx and ctx.cancel_event is not None and ctx.cancel_event.is_set())


def check_cancelled() -> None:
    if is_cancelled():
        raise LLMCancelled("Cancelled")


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str):
    """Parse JSON out of a model reply that may be wrapped in markdown fences,
    <think> blocks, or surrounding prose."""
    if raw is None:
        raise ValueError("empty reply")
    s = _THINK_RE.sub("", raw).strip()
    if not s:
        raise ValueError("empty reply")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for m in _FENCE_RE.finditer(s):
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    attempts = 0
    for i, ch in enumerate(s):
        if ch in "[{":
            attempts += 1
            try:
                obj, _end = decoder.raw_decode(s, i)
                return obj
            except json.JSONDecodeError:
                if attempts > 60:
                    break
                continue
    raise ValueError("no JSON value found in reply")


def wrap_array_schema(schema: dict | None) -> tuple[dict | None, bool]:
    """Several structured-output APIs require an object at the root. Wrap a
    root array as {"items": [...]}; chat_json unwraps it again."""
    if schema and schema.get("type") == "array":
        return {"type": "object", "properties": {"items": schema}, "required": ["items"]}, True
    return schema, False


def strict_schema(schema: dict) -> dict:
    """OpenAI strict structured outputs: every object must list all properties
    as required and forbid additional ones."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.setdefault("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
                for p in props.values():
                    walk(p)
            if "items" in node:
                walk(node["items"])
        elif isinstance(node, list):
            for n in node:
                walk(n)

    walk(s)
    return s


def coerce_to_schema(result, schema: dict | None):
    """Repair the most common shape mistakes (array collapsed to an object,
    array wrapped in {"items": ...}, single object wrapped in a list)."""
    if schema is None:
        return result
    t = schema.get("type")
    if t == "array":
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            lists = [v for v in result.values() if isinstance(v, list)]
            if len(lists) == 1 and (not lists[0] or isinstance(lists[0][0], dict) or len(result) == 1):
                return lists[0]
            return [result]
    if t == "object" and isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        return result[0]
    return result


def flatten_messages(messages: list[dict]) -> tuple[str, str]:
    """For single-shot backends (CLIs): returns (system_text, prompt_text),
    with any prior conversation rendered as a transcript."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [m for m in messages if m["role"] in ("user", "assistant")]
    if len(convo) == 1:
        return system, convo[0]["content"]
    parts = []
    for m in convo[:-1]:
        who = "AUTHOR" if m["role"] == "user" else "YOU (assistant)"
        parts.append(f"### {who}:\n{m['content']}")
    transcript = "\n\n".join(parts)
    last = convo[-1]["content"] if convo else ""
    return system, (f"Conversation so far:\n\n{transcript}\n\n### AUTHOR (latest message, reply to this):\n{last}")


def schema_instruction(schema: dict) -> str:
    return ("\n\nRespond with ONLY valid JSON (no markdown fences, no commentary) that "
            "conforms to this JSON Schema:\n" + json.dumps(schema))


def safe_model_name(model: str | None) -> str | None:
    if model in (None, "", "auto", "default"):
        return None
    if not _SAFE_MODEL_RE.match(model):
        raise LLMError(f"Invalid model name: {model!r}")
    return model


class BaseLLM:
    backend = "base"
    label = "base"
    is_local = False
    max_parallel = 1
    supports_native_schema = False

    @property
    def supports_images(self) -> bool:
        """Can this backend/model look at figure images passed in a message's
        "images" list? Backends that can't simply get the text."""
        return False

    def __init__(self, model: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.model = safe_model_name(model)
        self.timeout = timeout

    # --- to implement -----------------------------------------------------
    def _chat(self, messages: list[dict], temperature: float, json_schema: dict | None,
              think: bool | None) -> str:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True

    @property
    def context_chars(self) -> int:
        """How many characters of document material fit comfortably in one
        prompt (leaving room for instructions and the reply)."""
        return 300_000

    # --- shared -----------------------------------------------------------
    def describe(self) -> dict:
        return {"backend": self.backend, "model": self.model or "(default)",
                "label": self.label, "is_local": self.is_local}

    @property
    def display_name(self) -> str:
        return f"{self.backend}:{self.model or 'default'}"

    def chat(self, messages: list[dict], temperature: float = 0.4,
             json_schema: dict | None = None, think: bool | None = None,
             json_mode: bool = False) -> str:
        check_cancelled()
        if any(m.get("images") for m in messages) and not self.supports_images:
            messages = strip_images(messages)
        if json_schema is not None and not self.supports_native_schema:
            messages = append_to_last_user(messages, schema_instruction(json_schema))
        elif json_mode and json_schema is None and not self.supports_native_schema:
            messages = append_to_last_user(messages, "\n\nRespond with ONLY valid JSON.")
        chars_in = sum(len(m.get("content") or "") for m in messages)
        t0 = time.monotonic()
        ok = False
        out = ""
        try:
            out = self._chat(messages, temperature, json_schema, think)
            ok = True
            return out
        finally:
            ctx = current_context()
            if ctx is not None:
                ctx.record(chars_in, len(out or ""), time.monotonic() - t0, ok)

    def chat_json(self, messages: list[dict], temperature: float = 0.2,
                  json_schema: dict | None = None, retries: int = 2,
                  think: bool | None = None):
        """Chat expecting JSON back. Pass json_schema to force an exact shape;
        malformed replies are retried with the parser error as feedback."""
        attempt_messages = list(messages)
        last_error: Exception | None = None
        for _attempt in range(retries + 1):
            raw = self.chat(attempt_messages, temperature=temperature,
                            json_schema=json_schema, think=think, json_mode=True)
            try:
                return coerce_to_schema(extract_json(raw), json_schema)
            except ValueError as e:
                last_error = e
                attempt_messages = list(messages) + [
                    {"role": "assistant", "content": (raw or "")[:4000]},
                    {"role": "user", "content": (
                        "That was not valid JSON. Respond with ONLY valid JSON, no markdown "
                        f"fences, no commentary. Parser error: {e}")},
                ]
        raise LLMError(f"Model did not return valid JSON after {retries + 1} attempts: {last_error}")


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_IMAGE_BYTES = 4_500_000


def image_b64(path: str) -> tuple[str, str]:
    """(media type, base64 data) for an image file a model can read."""
    mime = IMAGE_TYPES.get(os.path.splitext(path)[1].lower()) or mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        return mime, base64.b64encode(f.read()).decode("ascii")


def usable_images(m: dict) -> list[str]:
    return [p for p in (m.get("images") or [])
            if os.path.splitext(p)[1].lower() in IMAGE_TYPES and os.path.isfile(p)
            and os.path.getsize(p) <= MAX_IMAGE_BYTES]


def strip_images(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        m = dict(m)
        if m.pop("images", None):
            m["content"] = (m.get("content") or "") + (
                "\n\n[The figure image itself is not available to you; judge the figure from its caption, "
                "the text and the code that draws it.]")
        out.append(m)
    return out


def append_to_last_user(messages: list[dict], suffix: str) -> list[dict]:
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m["role"] == "user":
            m["content"] = (m.get("content") or "") + suffix
            break
    return out


def http_json(url: str, payload: dict | None, headers: dict | None = None,
               timeout: float = 600, method: str | None = None, retries: int = 3) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    delay = 4.0
    for attempt in range(retries + 1):
        check_cancelled()
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < retries:
                retry_after = e.headers.get("retry-after") if e.headers else None
                try:
                    wait = min(float(retry_after), 60.0) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(wait)
                delay *= 2
                continue
            raise LLMHTTPError(e.code, body, url.split("?")[0]) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise LLMError(f"Could not reach {url.split('?')[0]}: {e}") from e
    raise LLMError("unreachable")
