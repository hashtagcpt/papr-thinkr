"""Picking a backend by name, and detecting which backends this machine can
use right now."""
from __future__ import annotations

import os
import re

from .base import DEFAULT_TIMEOUT, BaseLLM, LLMError
from .cli_agents import ClaudeCodeCLI, CodexCLI, GeminiCLI
from .cloud_apis import AnthropicAPI, GeminiAPI, OpenAIAPI
from .ollama import OllamaLLM


BACKENDS: dict[str, type[BaseLLM]] = {
    "ollama": OllamaLLM,
    "claude-code": ClaudeCodeCLI,
    "codex": CodexCLI,
    "gemini": GeminiCLI,
    "anthropic-api": AnthropicAPI,
    "openai-api": OpenAIAPI,
    "gemini-api": GeminiAPI,
}


ALIASES = {
    "local": "ollama", "claude": "claude-code", "claudecode": "claude-code",
    "claude-cli": "claude-code", "codex-cli": "codex", "gemini-cli": "gemini",
    "anthropic": "anthropic-api", "openai": "openai-api", "lmstudio": "openai-api",
    "google": "gemini-api",
}


def canonical_backend(name: str | None) -> str:
    n = (name or "ollama").strip().lower()
    n = ALIASES.get(n, n)
    if n not in BACKENDS:
        raise LLMError(f"Unknown backend '{name}'. Choose one of: {', '.join(BACKENDS)}")
    return n


def make_llm(backend: str | None = "ollama", model: str | None = None, **opts) -> BaseLLM:
    """opts: ollama_host, num_ctx, base_url, timeout (unknown keys ignored)."""
    b = canonical_backend(backend)
    timeout = int(opts.get("timeout") or DEFAULT_TIMEOUT)
    if b == "ollama":
        return OllamaLLM(model, host=opts.get("ollama_host"), num_ctx=opts.get("num_ctx"), timeout=timeout)
    if b == "openai-api":
        return OpenAIAPI(model, timeout=timeout, base_url=opts.get("base_url"))
    return BACKENDS[b](model, timeout=timeout)


def detect_backends(ollama_host: str | None = None) -> list[dict]:
    """What can be used on this machine right now. Cheap: no model calls."""
    out = []
    ol = OllamaLLM(host=ollama_host)
    records = ol.model_records(refresh=True)
    reachable = bool(records) or ol.is_available()
    chat_models = ol.list_models() if reachable else []
    out.append({
        "id": "ollama", "label": "Ollama (local)", "kind": "local",
        "available": reachable and bool(chat_models),
        "detail": (f"{len(chat_models)} local chat model(s) at {ol.host}" if chat_models else
                   ("Ollama is running but no chat models are pulled" if reachable else
                    f"Ollama not reachable at {ol.host} (start it with `ollama serve`)")),
        "models": chat_models,
        "default_model": OllamaLLM.pick_default(records) if records else None,
    })
    for cls in (ClaudeCodeCLI, CodexCLI, GeminiCLI):
        exe = cls.find_executable()
        problem = cls.auth_problem() if exe else None
        out.append({
            "id": cls.backend, "label": cls.label, "kind": "cloud-cli", "available": bool(exe) and not problem,
            "detail": (f"`{cls.exe_name}` CLI not installed" if not exe else
                       f"installed, but {problem}" if problem else
                       f"uses your existing `{cls.exe_name}` login ({exe})"),
            "models": ([m for m in [CodexCLI.configured_model()] if m] + list(cls.MODEL_HINTS)
                       if cls is CodexCLI else list(cls.MODEL_HINTS)),
            "default_model": CodexCLI.configured_model() if cls is CodexCLI else None,
        })
    for cls, env in ((AnthropicAPI, "ANTHROPIC_API_KEY"), (OpenAIAPI, "OPENAI_API_KEY"),
                     (GeminiAPI, "GEMINI_API_KEY / GOOGLE_API_KEY")):
        present = cls.key_present()
        default = getattr(cls, "DEFAULT_MODEL", None)
        out.append({
            "id": cls.backend, "label": cls.label, "kind": "cloud-api", "available": present,
            "detail": f"{env} {'is set' if present else 'not set'}",
            "models": list(cls.MODEL_HINTS), "default_model": default,
        })
    base = os.environ.get("OPENAI_BASE_URL", "")
    if re.match(r"^https?://(127\.0\.0\.1|localhost)", base):
        for d in out:
            if d["id"] == "openai-api":
                d.update(available=True, kind="local", detail=f"local OpenAI-compatible server at {base}")
    return out
