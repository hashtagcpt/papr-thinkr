"""Thin client for a local Ollama server. Stdlib only (no pip dependency)."""
from __future__ import annotations

import json
import urllib.request
import urllib.error

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "gemma3:27b-it-qat"
DEFAULT_NUM_CTX = 16384


class LLMError(RuntimeError):
    pass


class OllamaClient:
    """Talks to a local Ollama daemon over its REST API."""

    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                 num_ctx: int = DEFAULT_NUM_CTX, timeout: int = 300):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise LLMError(
                f"Could not reach Ollama at {self.host}. Is it running? (ollama serve) Detail: {e}"
            ) from e

    def is_available(self) -> bool:
        try:
            url = f"{self.host}/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            url = f"{self.host}/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def chat(self, messages: list[dict], json_mode: bool = False,
              json_schema: dict | None = None,
              temperature: float = 0.4, model: str | None = None) -> str:
        """messages: list of {"role": "system"|"user"|"assistant", "content": str}"""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": temperature,
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema
        elif json_mode:
            payload["format"] = "json"
        result = self._post("/api/chat", payload)
        if "error" in result:
            raise LLMError(result["error"])
        return result.get("message", {}).get("content", "")

    def chat_json(self, messages: list[dict], temperature: float = 0.2,
                  model: str | None = None, retries: int = 2,
                  json_schema: dict | None = None) -> dict | list:
        """Chat expecting a JSON object/array back. Pass json_schema (a JSON
        Schema dict) to force an exact shape (e.g. an array of objects) --
        plain format="json" mode will happily collapse a requested array down
        to a single object if the model feels like it. Retries with
        corrective feedback if the model returns malformed JSON."""
        attempt_messages = list(messages)
        last_error = None
        for attempt in range(retries + 1):
            raw = self.chat(attempt_messages, json_mode=(json_schema is None),
                             json_schema=json_schema, temperature=temperature, model=model)
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                last_error = e
                attempt_messages = list(messages) + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": (
                        "That was not valid JSON. Respond with ONLY valid JSON, "
                        "no markdown fences, no commentary. "
                        f"JSON parser error: {e}"
                    )},
                ]
        raise LLMError(f"Model did not return valid JSON after {retries + 1} attempts: {last_error}")
