"""Cloud models through their HTTP APIs, with keys from the environment:
Anthropic (ANTHROPIC_API_KEY), OpenAI (OPENAI_API_KEY; OPENAI_BASE_URL also
points it at a *local* OpenAI-compatible server such as LM Studio), and
Google Gemini (GEMINI_API_KEY or GOOGLE_API_KEY)."""
from __future__ import annotations

import json
import os
import re

from .base import (DEFAULT_TIMEOUT, BaseLLM, LLMError, LLMHTTPError, append_to_last_user, extract_json,
                   http_json, image_b64, schema_instruction, strict_schema, usable_images, wrap_array_schema)


class AnthropicAPI(BaseLLM):
    backend = "anthropic-api"
    label = "Anthropic API"
    max_parallel = 4
    DEFAULT_MODEL = "claude-opus-5-5"
    MODEL_HINTS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"]

    def __init__(self, model: str | None = None, timeout: int = DEFAULT_TIMEOUT,
                 api_key: str | None = None, max_tokens: int = 16000):
        super().__init__(model or os.environ.get("ANTHROPIC_MODEL") or self.DEFAULT_MODEL, timeout)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    @staticmethod
    def key_present() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def is_available(self) -> bool:
        return bool(self.api_key)

    @property
    def context_chars(self) -> int:
        return 500_000

    @property
    def supports_images(self) -> bool:
        return True

    def _chat(self, messages, temperature, json_schema, think):
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs: list[dict] = []
        for m in messages:
            if m["role"] not in ("user", "assistant"):
                continue
            blocks = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
                      for mt, data in (image_b64(p) for p in usable_images(m))]
            blocks.append({"type": "text", "text": m["content"]})
            if msgs and msgs[-1]["role"] == m["role"]:
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": m["role"], "content": blocks})
        if not msgs or msgs[0]["role"] != "user":
            msgs.insert(0, {"role": "user", "content": [{"type": "text", "text": "(begin)"}]})
        payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": msgs,
                   "temperature": temperature}
        if system:
            payload["system"] = system
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        url = f"{self.base_url}/v1/messages"
        try:
            data = http_json(url, payload, headers, timeout=self.timeout)
        except LLMHTTPError as e:
            if e.status == 400 and "temperature" in e.body:
                payload.pop("temperature", None)
                data = http_json(url, payload, headers, timeout=self.timeout)
            else:
                raise
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OpenAIAPI(BaseLLM):
    backend = "openai-api"
    label = "OpenAI API (or local OpenAI-compatible server)"
    max_parallel = 4
    supports_native_schema = True
    DEFAULT_MODEL = "gpt-5"
    MODEL_HINTS = ["gpt-5", "gpt-5-mini"]

    def __init__(self, model: str | None = None, timeout: int = DEFAULT_TIMEOUT,
                 api_key: str | None = None, base_url: str | None = None):
        super().__init__(model or os.environ.get("OPENAI_MODEL") or self.DEFAULT_MODEL, timeout)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.is_local = bool(re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])", self.base_url))

    @staticmethod
    def key_present() -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def is_available(self) -> bool:
        return bool(self.api_key) or self.is_local

    @property
    def context_chars(self) -> int:
        return 120_000 if self.is_local else 350_000

    @property
    def supports_images(self) -> bool:
        return not self.is_local     # local servers often reject image content

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            images = usable_images(m)
            if not images:
                out.append({"role": m["role"], "content": m["content"]})
                continue
            parts = [{"type": "text", "text": m["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}}
                      for mt, data in (image_b64(p) for p in images)]
            out.append({"role": m["role"], "content": parts})
        return out

    def _chat(self, messages, temperature, json_schema, think):
        if not self.api_key and not self.is_local:
            raise LLMError("OPENAI_API_KEY is not set.")
        payload: dict = {"model": self.model,
                         "messages": self._messages(messages)}
        if self.is_local:
            payload["temperature"] = temperature   # official reasoning models reject temperature
        wrapped, was_wrapped = wrap_array_schema(json_schema)
        if wrapped is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "result", "schema": strict_schema(wrapped), "strict": True}}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.base_url}/chat/completions"
        try:
            data = http_json(url, payload, headers, timeout=self.timeout)
        except LLMHTTPError as e:
            if e.status == 400 and "response_format" in payload:
                payload.pop("response_format")
                payload["messages"] = self._messages(append_to_last_user(messages, schema_instruction(json_schema)))
                data = http_json(url, payload, headers, timeout=self.timeout)
            else:
                raise
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        if msg.get("refusal"):
            raise LLMError(f"Model refused: {msg['refusal']}")
        text = msg.get("content") or ""
        if was_wrapped:
            try:
                obj = extract_json(text)
                if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                    return json.dumps(obj["items"])
            except ValueError:
                pass
        return text


class GeminiAPI(BaseLLM):
    backend = "gemini-api"
    label = "Google Gemini API"
    max_parallel = 4
    supports_native_schema = True
    FALLBACK_MODEL = "gemini-3.8-flash"
    MODEL_HINTS = ["gemini-3.1-pro-preview", "gemini-3.8-flash"]
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: str | None = None, timeout: int = DEFAULT_TIMEOUT,
                 api_key: str | None = None):
        super().__init__(model or os.environ.get("GEMINI_MODEL"), timeout)
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._resolved: str | None = None

    @staticmethod
    def key_present() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def is_available(self) -> bool:
        return bool(self.api_key)

    @property
    def context_chars(self) -> int:
        return 900_000

    @property
    def supports_images(self) -> bool:
        return True

    def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            data = http_json(f"{self.BASE}/models?pageSize=1000", None,
                              {"x-goog-api-key": self.api_key}, timeout=20, retries=1)
        except LLMError:
            return []
        names = []
        for m in data.get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = m.get("name", "").split("/", 1)[-1]
            if re.search(r"(tts|image|embed|live|audio|aqa|robotics|computer-use|native)", name):
                continue
            if name.startswith("gemini"):
                names.append(name)
        return names

    @staticmethod
    def _rank(name: str) -> tuple:
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        v = float(ver.group(1)) if ver else 0.0
        tier = 2 if "-pro" in name else (1 if "flash" in name and "lite" not in name else 0)
        return (tier, v, "preview" not in name and "exp" not in name)

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        if not self._resolved:
            names = self.list_models()
            self._resolved = max(names, key=self._rank) if names else self.FALLBACK_MODEL
        return self._resolved

    @property
    def display_name(self) -> str:
        return f"gemini-api:{self.model or self._resolved or 'auto'}"

    def _chat(self, messages, temperature, json_schema, think):
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set.")
        model = self.resolve_model()
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "user" if m["role"] == "user" else "model",
                     "parts": [{"text": m["content"]}] + [{"inline_data": {"mime_type": mt, "data": data}}
                                                         for mt, data in (image_b64(p) for p in usable_images(m))]}
                    for m in messages if m["role"] in ("user", "assistant")]
        gen: dict = {"temperature": temperature}
        if json_schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseJsonSchema"] = json_schema
        payload: dict = {"contents": contents, "generationConfig": gen}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self.BASE}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.api_key}
        try:
            data = http_json(url, payload, headers, timeout=self.timeout)
        except LLMHTTPError as e:
            if e.status == 400 and "responseJsonSchema" in gen:
                gen.pop("responseJsonSchema")
                payload["contents"] = [dict(c) for c in contents]
                payload["contents"][-1] = {"role": "user", "parts": [
                    {"text": contents[-1]["parts"][0]["text"] + schema_instruction(json_schema)}]
                    + contents[-1]["parts"][1:]}
                data = http_json(url, payload, headers, timeout=self.timeout)
            else:
                raise
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback") or {}
            raise LLMError(f"Gemini returned no candidates (blocked? {fb.get('blockReason', 'unknown')})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
