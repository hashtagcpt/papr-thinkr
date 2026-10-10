"""Local models through an Ollama daemon (https://ollama.com)."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .base import (DEFAULT_TIMEOUT, BaseLLM, LLMCancelled, LLMError, LLMHTTPError, http_json, image_b64,
                   is_cancelled, usable_images)

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"   # not "localhost": on Windows that
                                                  # costs ~2 s/request (IPv6 fallback)
DEFAULT_NUM_CTX = 32768


# Preferred local models, best first. The first one that is actually pulled
# wins; otherwise the largest installed chat model is used.
LOCAL_MODEL_PREFERENCE = [
    "qwen3.6:27b", "gemma4:31b", "qwen3.6:latest", "gemma4:26b", "qwen3:32b",
    "qwen3:30b", "gemma3:27b-it-qat", "gemma3:27b", "gpt-oss:20b", "qwen3:14b",
    "gemma4:latest", "gemma3:12b", "llama3.3:70b", "llama3.1:8b",
]


def normalize_host(host: str) -> str:
    host = (host or DEFAULT_OLLAMA_HOST).strip()
    if "://" not in host:
        host = "http://" + host
    host = host.rstrip("/")
    host = host.replace("://0.0.0.0", "://127.0.0.1").replace("://localhost", "://127.0.0.1")
    if re.match(r"^https?://[^/:]+$", host):
        host += ":11434"
    return host


class OllamaLLM(BaseLLM):
    backend = "ollama"
    label = "Ollama (local)"
    is_local = True
    max_parallel = 1          # one GPU: parallel requests just queue (and break prefix caching)
    supports_native_schema = True

    def __init__(self, model: str | None = None, host: str | None = None,
                 num_ctx: int | None = None, timeout: int = DEFAULT_TIMEOUT,
                 keep_alive: str = "30m"):
        super().__init__(model, timeout)
        self.host = normalize_host(host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST)
        self.requested_num_ctx = int(num_ctx or os.environ.get("PAPR_NUM_CTX") or DEFAULT_NUM_CTX)
        self.keep_alive = keep_alive
        self._models_cache: tuple[float, list[dict]] | None = None
        self._resolved_model: str | None = None
        self._spill_warned = False
        self.warnings: list[str] = []

    # -- discovery --
    def _get(self, path: str, timeout: float = 5) -> dict:
        with urllib.request.urlopen(f"{self.host}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def is_available(self) -> bool:
        try:
            self._get("/api/version", timeout=3)
            return True
        except Exception:
            return False

    def model_records(self, refresh: bool = False) -> list[dict]:
        now = time.monotonic()
        if not refresh and self._models_cache and now - self._models_cache[0] < 30:
            return self._models_cache[1]
        try:
            data = self._get("/api/tags")
            models = data.get("models", [])
        except Exception:
            models = []
        self._models_cache = (now, models)
        return models

    def list_models(self) -> list[str]:
        """Chat-capable models (embedding-only models are excluded)."""
        out = []
        for m in self.model_records():
            caps = m.get("capabilities")
            if caps is not None and "completion" not in caps:
                continue
            if caps is None and "embed" in m.get("name", ""):
                continue
            out.append(m["name"])
        return out

    @staticmethod
    def pick_default(records: list[dict]) -> str | None:
        names = []
        for m in records:
            caps = m.get("capabilities")
            if caps is not None and "completion" not in caps:
                continue
            if m.get("remote_host") or m.get("name", "").endswith("-cloud"):
                continue   # keep auto-pick truly local
            names.append(m["name"])
        for pref in LOCAL_MODEL_PREFERENCE:
            if pref in names:
                return pref
        if not names:
            return None
        by_size = sorted((m for m in records if m.get("name") in names),
                         key=lambda m: m.get("size", 0), reverse=True)
        return by_size[0]["name"]

    def resolve_model(self) -> str:
        if self._resolved_model:
            return self._resolved_model
        records = self.model_records()
        names = [m["name"] for m in records]
        if self.model:
            if names and self.model not in names and f"{self.model}:latest" not in names:
                raise LLMError(
                    f"Ollama model '{self.model}' is not pulled. Installed: {', '.join(names) or 'none'}. "
                    f"Pull it with: ollama pull {self.model}")
            self._resolved_model = self.model
        else:
            picked = self.pick_default(records)
            if not picked:
                raise LLMError("No chat models are installed in Ollama. Pull one, e.g. "
                               "`ollama pull qwen3.6:27b`, or choose a cloud backend.")
            self._resolved_model = picked
        return self._resolved_model

    def _model_context_length(self) -> int | None:
        name = self.resolve_model()
        for m in self.model_records():
            if m.get("name") == name:
                cl = (m.get("details") or {}).get("context_length")
                if cl:
                    return int(cl)
        try:
            info = http_json(f"{self.host}/api/show", {"model": name}, timeout=10, retries=0)
            for k, v in (info.get("model_info") or {}).items():
                if k.endswith(".context_length"):
                    return int(v)
        except Exception:
            pass
        return None

    @property
    def num_ctx(self) -> int:
        # Fixed per session on purpose: changing num_ctx between calls makes
        # Ollama reload the whole model.
        limit = self._model_context_length() or self.requested_num_ctx
        return max(4096, min(self.requested_num_ctx, limit))

    @property
    def context_chars(self) -> int:
        try:
            ctx = self.num_ctx
        except LLMError:
            ctx = self.requested_num_ctx
        # leave ~6k tokens for instructions + reply (+thinking); ~3 chars/token
        return max(8000, int((ctx - 6144) * 3.0))

    @property
    def display_name(self) -> str:
        try:
            return f"ollama:{self.resolve_model()}"
        except LLMError:
            return f"ollama:{self.model or 'auto'}"

    def describe(self) -> dict:
        d = super().describe()
        try:
            d["model"] = self.resolve_model()
            d["num_ctx"] = self.num_ctx
        except LLMError as e:
            d["error"] = str(e)
        d["host"] = self.host
        return d

    # -- chat --
    def _chat(self, messages, temperature, json_schema, think):
        model = self.resolve_model()
        payload = {
            "model": model,
            "messages": [self._message(m) for m in messages],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "temperature": temperature},
        }
        if json_schema is not None:
            payload["format"] = json_schema
        if think is not None:
            payload["think"] = think
        try:
            content = self._stream_chat(payload)
        except LLMHTTPError as e:
            if "think" in payload and e.status == 400 and "think" in e.body.lower():
                payload.pop("think")       # model doesn't support toggling thinking
                content = self._stream_chat(payload)
            else:
                raise
        self._check_gpu_spill(model)
        return content

    @property
    def supports_images(self) -> bool:
        try:
            name = self.resolve_model()
        except LLMError:
            return False
        for m in self.model_records():
            if m.get("name") == name:
                return "vision" in (m.get("capabilities") or [])
        return False

    @staticmethod
    def _message(m: dict) -> dict:
        out = {"role": m["role"], "content": m["content"]}
        images = usable_images(m)
        if images:
            out["images"] = [image_b64(p)[1] for p in images]
        return out

    def _stream_chat(self, payload: dict) -> str:
        url = f"{self.host}/api/chat"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        parts: list[str] = []
        done_reason = None
        try:
            # timeout here is per socket read, so long generations are fine as
            # long as tokens keep flowing; prompt evaluation of a long paper can
            # take a few minutes before the first token.
            with urllib.request.urlopen(req, timeout=min(self.timeout, 900)) as resp:
                for line in resp:
                    if is_cancelled():
                        raise LLMCancelled("Cancelled")
                    line = line.strip()
                    if not line:
                        continue
                    chunk = json.loads(line.decode("utf-8"))
                    if chunk.get("error"):
                        raise LLMError(f"Ollama error: {chunk['error']}")
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        parts.append(msg["content"])
                    if chunk.get("done"):
                        done_reason = chunk.get("done_reason")
                        break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(body).get("error", body)
            except Exception:
                pass
            raise LLMHTTPError(e.code, body, url) from e
        except urllib.error.URLError as e:
            raise LLMError(f"Could not reach Ollama at {self.host}. Is it running? (ollama serve) "
                           f"Detail: {e}") from e
        except (TimeoutError, ConnectionError, OSError) as e:
            raise LLMError(f"Ollama request failed or timed out ({e}). The model may be too slow "
                           f"for this prompt size -- try a smaller model or --num-ctx.") from e
        text = "".join(parts)
        if done_reason == "length":
            self.warnings.append("A reply hit the context limit and was truncated; consider a larger --num-ctx.")
        return text

    def _check_gpu_spill(self, model: str) -> None:
        if self._spill_warned:
            return
        try:
            for m in self._get("/api/ps", timeout=3).get("models", []):
                if m.get("name") == model and m.get("size_vram") is not None and m.get("size"):
                    if m["size_vram"] < 0.9 * m["size"]:
                        pct = 100 * m["size_vram"] / m["size"]
                        msg = (f"Ollama is running {model} only {pct:.0f}% on GPU at num_ctx={self.num_ctx}; "
                               f"expect slow responses. A smaller --num-ctx or model will be faster.")
                        self.warnings.append(msg)
                        print("WARNING: " + msg, file=sys.stderr)
                    self._spill_warned = True
        except Exception:
            pass
