"""LLM backends for papr-thinkr. Stdlib only (no pip dependencies).

Local:
  ollama          any chat model pulled into a local Ollama daemon
  openai-api      also works against a *local* OpenAI-compatible server
                  (LM Studio, llama.cpp, vLLM) via --base-url / OPENAI_BASE_URL

Cloud, via the vendor's own command-line tool in headless mode (reuses
whatever login/subscription that CLI already has -- no API key needed here):
  claude-code     `claude -p`        (Claude Code)
  codex           `codex exec`       (OpenAI Codex CLI)
  gemini          `gemini -p`        (Google Gemini CLI)

Cloud, via HTTP API with a key from the environment:
  anthropic-api   ANTHROPIC_API_KEY
  openai-api      OPENAI_API_KEY
  gemini-api      GEMINI_API_KEY or GOOGLE_API_KEY

Every backend exposes the same chat() / chat_json() calls, so the rest of the
code never needs to know which one is in use. The agentic CLIs are always run
with tools disabled (or a read-only sandbox) in an empty scratch directory: a
manuscript or reviewer letter is untrusted input, and a prompt-injected
document must not be able to make an agent run commands or edit files.

Modules: base.py (shared interface), ollama.py (local), cli_agents.py (Claude
Code / Codex / Gemini CLIs), cloud_apis.py (HTTP APIs), registry.py (pick a
backend by name, detect what's installed).
"""
from .base import (BaseLLM, CallContext, LLMCancelled, LLMError, LLMHTTPError, check_cancelled,
                   coerce_to_schema, current_context, extract_json, flatten_messages, safe_model_name,
                   strict_schema, use_context, wrap_array_schema)
from .cli_agents import ClaudeCodeCLI, CodexCLI, GeminiCLI, run_cli
from .cloud_apis import AnthropicAPI, GeminiAPI, OpenAIAPI
from .ollama import OllamaLLM
from .registry import ALIASES, BACKENDS, canonical_backend, detect_backends, make_llm

__all__ = [
    "BaseLLM", "CallContext", "LLMCancelled", "LLMError", "LLMHTTPError", "check_cancelled",
    "coerce_to_schema", "current_context", "extract_json", "flatten_messages", "safe_model_name",
    "strict_schema", "use_context", "wrap_array_schema", "ClaudeCodeCLI", "CodexCLI", "GeminiCLI",
    "run_cli", "AnthropicAPI", "GeminiAPI", "OpenAIAPI", "OllamaLLM", "ALIASES", "BACKENDS",
    "canonical_backend", "detect_backends", "make_llm",
]
