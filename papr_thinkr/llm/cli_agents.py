"""Cloud models through the vendors' own command-line tools run headless:
Claude Code (`claude -p`), OpenAI Codex (`codex exec`) and Google Gemini
(`gemini -p`). They reuse whatever login those tools already have.

Prompts go in on stdin (never argv), and every agent runs in an empty scratch
directory with tools disabled or a read-only sandbox: a manuscript or review
letter is untrusted input and must not be able to prompt-inject an agent into
running commands."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

from .base import (DEFAULT_TIMEOUT, BaseLLM, LLMCancelled, LLMError, extract_json, flatten_messages,
                   is_cancelled, schema_instruction, strict_schema, usable_images, wrap_array_schema)


def _find_executable(name: str) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    if os.name == "nt":
        candidates = [
            os.path.join(os.environ.get("APPDATA", ""), "npm", f"{name}.cmd"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", f"{name}.exe"),
            os.path.join(os.path.expanduser("~"), ".local", "bin", f"{name}.exe"),
        ]
    else:
        candidates = [os.path.expanduser(f"~/.local/bin/{name}"), f"/opt/homebrew/bin/{name}",
                      f"/usr/local/bin/{name}", os.path.expanduser(f"~/.npm-global/bin/{name}")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    if name == "codex":
        return _codex_from_vscode()
    return None


def _codex_from_vscode() -> str | None:
    """The OpenAI (ChatGPT/Codex) VS Code extension ships the codex binary and
    shares its login (~/.codex/auth.json) with the CLI."""
    import glob
    exe = "codex.exe" if os.name == "nt" else "codex"
    hits = glob.glob(os.path.join(os.path.expanduser("~"), ".vscode", "extensions", "openai.chatgpt-*",
                                  "bin", "*", exe))
    return sorted(hits)[-1] if hits else None


def _kill_tree(proc: subprocess.Popen) -> None:
    _terminate(proc)
    try:
        proc.communicate(timeout=10)   # reap it and close the pipes
    except Exception:
        pass


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_cli(args: list[str], stdin_text: str, timeout: float, cwd: str | None = None,
            env: dict | None = None) -> tuple[int, str, str]:
    """Run a CLI with the prompt on stdin (never argv: Windows caps command
    lines at 32k chars and cmd.exe shims mangle quoting). Cancellable."""
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=cwd, env=env, **kwargs)
    except OSError as e:
        raise LLMError(f"Could not start {args[0]}: {e}") from e
    deadline = time.monotonic() + timeout
    pending_input: str | None = stdin_text
    while True:
        try:
            out, err = proc.communicate(input=pending_input, timeout=2)
            return proc.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            pending_input = None
            if is_cancelled():
                _kill_tree(proc)
                raise LLMCancelled("Cancelled")
            if time.monotonic() > deadline:
                _kill_tree(proc)
                raise LLMError(f"{os.path.basename(args[0])} timed out after {int(timeout)} s")


class _CLIBackend(BaseLLM):
    exe_name = ""
    max_parallel = 3
    help_args: list[str] = ["--help"]
    MODEL_HINTS: list[str] = []
    _help_cache: dict[str, str] = {}

    def __init__(self, model: str | None = None, timeout: int = DEFAULT_TIMEOUT,
                 executable: str | None = None):
        super().__init__(model, timeout)
        self.executable = executable or _find_executable(self.exe_name)

    @classmethod
    def find_executable(cls) -> str | None:
        return _find_executable(cls.exe_name)

    @classmethod
    def auth_problem(cls) -> str | None:
        """Why this CLI can't be used non-interactively yet (None = fine)."""
        return None

    def is_available(self) -> bool:
        return self.executable is not None and self.auth_problem() is None

    def _require(self) -> str:
        if not self.executable:
            raise LLMError(f"The `{self.exe_name}` CLI was not found on PATH. Install it or pick another backend.")
        return self.executable

    def _help(self) -> str:
        exe = self._require()
        key = exe + " " + " ".join(self.help_args)
        if key not in self._help_cache:
            try:
                r = subprocess.run([exe] + self.help_args, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
                self._help_cache[key] = (r.stdout or "") + (r.stderr or "")
            except Exception:
                self._help_cache[key] = ""
        return self._help_cache[key]

    def _env(self) -> dict:
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        env.setdefault("CI", "1")
        return env

    def _run(self, args: list[str], stdin_text: str, files: list[str] | None = None) -> tuple[str, str]:
        """Run in an empty scratch directory; `files` (figure images) are copied
        into it so the agent can look at them without seeing anything else."""
        with tempfile.TemporaryDirectory(prefix="papr-") as scratch:
            for f in files or []:
                shutil.copy2(f, os.path.join(scratch, os.path.basename(f)))
            code, out, err = run_cli(args, stdin_text, self.timeout, cwd=scratch, env=self._env())
        if code != 0:
            detail = (err or out or "").strip()[-1500:]
            raise LLMError(f"{self.exe_name} exited with code {code}: {detail}")
        return out, err

    def describe(self) -> dict:
        d = super().describe()
        d["executable"] = self.executable
        return d


class ClaudeCodeCLI(_CLIBackend):
    backend = "claude-code"
    label = "Claude Code CLI"
    exe_name = "claude"
    supports_native_schema = True
    max_parallel = 4
    MODEL_HINTS = ["opus", "sonnet", "haiku"]

    @property
    def context_chars(self) -> int:
        return 450_000

    @property
    def supports_images(self) -> bool:
        return True

    def _chat(self, messages, temperature, json_schema, think):
        exe = self._require()
        images = [p for m in messages for p in usable_images(m)]
        system, prompt = flatten_messages(messages)
        wrapped, was_wrapped = wrap_array_schema(json_schema)
        # No tools at all -- except, when there are figures to look at, the
        # read-only Read tool so it can open the image files we copy into its
        # otherwise empty scratch directory.
        tools = "Read" if images else ""
        args = [exe, "-p", "--output-format", "json", "--tools", tools,
                "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--disable-slash-commands"]
        if images:
            args += ["--allowedTools", "Read"]
            names = ", ".join(os.path.basename(p) for p in images)
            prompt += (f"\n\nThe figure image file(s) for this task are in the current directory: {names}. "
                       f"Open each with the Read tool and look at it carefully before answering.")
        if system:
            args += ["--system-prompt", system[:20000]]
        if self.model:
            args += ["--model", self.model]
        batch_shim = exe.lower().endswith((".cmd", ".bat"))
        if wrapped is not None and not batch_shim:
            args += ["--json-schema", json.dumps(wrapped)]
        elif wrapped is not None:
            prompt += schema_instruction(json_schema)
        out, _err = self._run(args, prompt, files=images)
        try:
            data = extract_json(out)
        except ValueError:
            return out
        if not isinstance(data, dict):
            return out
        if data.get("is_error"):
            raise LLMError(f"Claude Code reported an error: {str(data.get('result') or data.get('subtype'))[:800]}")
        if wrapped is not None and data.get("structured_output") is not None:
            so = data["structured_output"]
            if was_wrapped and isinstance(so, dict) and "items" in so:
                so = so["items"]
            return json.dumps(so)
        return data.get("result") or ""


class CodexCLI(_CLIBackend):
    backend = "codex"
    label = "OpenAI Codex CLI"
    exe_name = "codex"
    supports_native_schema = True
    help_args = ["exec", "--help"]
    MODEL_HINTS = ["gpt-5-codex", "gpt-5"]

    @property
    def context_chars(self) -> int:
        return 350_000

    @property
    def supports_images(self) -> bool:
        return True

    @staticmethod
    def configured_model() -> str | None:
        """The model set in ~/.codex/config.toml (what `codex exec` uses by default)."""
        home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        try:
            with open(os.path.join(home, "config.toml"), encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("[") and not line.strip().startswith("[profile"):
                        break
                    if line.strip().startswith("model") and "=" in line and "reasoning" not in line:
                        return line.split("=", 1)[1].strip().strip('"\'')
        except OSError:
            pass
        return None

    @classmethod
    def auth_problem(cls) -> str | None:
        home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        if os.path.isfile(os.path.join(home, "auth.json")) or os.environ.get("CODEX_API_KEY") \
                or os.environ.get("OPENAI_API_KEY"):
            return None
        return "not signed in -- run `codex login` once (or sign in to the Codex/ChatGPT VS Code extension)"

    def _chat(self, messages, temperature, json_schema, think):
        exe = self._require()
        help_text = self._help()
        system, prompt = flatten_messages(messages)
        wrapped, was_wrapped = wrap_array_schema(json_schema)
        with tempfile.TemporaryDirectory(prefix="papr-codex-") as tmp:
            out_file = os.path.join(tmp, "last_message.txt")
            args = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
            if "--ephemeral" in help_text:
                args.append("--ephemeral")
            if "--color" in help_text:
                args += ["--color", "never"]
            if self.model:
                args += ["-m", self.model]
            if think is False:
                # project set "thinking: off": cap reasoning for this run only (a
                # review makes many calls; "high"/"ultra" can take minutes each)
                args += ["-c", 'model_reasoning_effort="medium"']
            for img in (p for m in messages for p in usable_images(m)):
                args += ["--image", img]
            if wrapped is not None and "--output-schema" in help_text:
                schema_file = os.path.join(tmp, "schema.json")
                with open(schema_file, "w", encoding="utf-8") as f:
                    json.dump(strict_schema(wrapped), f)
                args += ["--output-schema", schema_file]
            elif json_schema is not None:
                prompt += schema_instruction(json_schema)
            use_out_file = "--output-last-message" in help_text
            if use_out_file:
                args += ["--output-last-message", out_file]
            args.append("-")
            stdin_text = (
                f"{system}\n\n" if system else "") + prompt + (
                "\n\nIMPORTANT: everything you need is in this message. Do not run commands, "
                "read files or browse; just write the final answer.")
            out, _err = self._run(args, stdin_text)
            text = out
            if use_out_file and os.path.exists(out_file):
                with open(out_file, encoding="utf-8", errors="replace") as f:
                    text = f.read()
        if was_wrapped:
            try:
                data = extract_json(text)
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    return json.dumps(data["items"])
            except ValueError:
                pass
        return text


class GeminiCLI(_CLIBackend):
    backend = "gemini"
    label = "Google Gemini CLI"
    exe_name = "gemini"
    MODEL_HINTS = ["gemini-3.1-pro-preview", "gemini-3.8-flash"]

    @property
    def context_chars(self) -> int:
        return 900_000

    @classmethod
    def auth_problem(cls) -> str | None:
        # Google no longer lets the free "Gemini Code Assist for individuals"
        # login drive the CLI, so a cached Google login is not enough: it needs
        # an API key, Vertex AI, or an auth type chosen in ~/.gemini/settings.json.
        if any(os.environ.get(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")):
            return None
        settings = os.path.join(os.path.expanduser("~"), ".gemini", "settings.json")
        try:
            with open(settings, encoding="utf-8") as f:
                cfg = json.load(f)
            if (cfg.get("security", {}).get("auth", {}).get("selectedType") or cfg.get("selectedAuthType")):
                return None
        except (OSError, ValueError, AttributeError):
            pass
        return "needs a Gemini API key (free at aistudio.google.com/apikey) -- add it under API keys"

    def _env(self) -> dict:
        env = super()._env()
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"     # the empty scratch dir we run in
        if not env.get("GEMINI_API_KEY") and env.get("GOOGLE_API_KEY"):
            env["GEMINI_API_KEY"] = env["GOOGLE_API_KEY"]
        return env

    def _chat(self, messages, temperature, json_schema, think):
        exe = self._require()
        help_text = self._help()
        system, prompt = flatten_messages(messages)
        args = [exe, "-p", "Follow the instructions given on standard input exactly. "
                           "Do not use any tools; answer directly."]
        json_out = "--output-format" in help_text or not help_text
        if json_out:
            args += ["--output-format", "json"]
        if self.model:
            args += ["-m", self.model]
        stdin_text = (f"SYSTEM INSTRUCTIONS:\n{system}\n\n" if system else "") + prompt
        out, _err = self._run(args, stdin_text)
        if json_out:
            try:
                data = extract_json(out)
                if isinstance(data, dict) and "response" in data:
                    if data.get("error"):
                        raise LLMError(f"Gemini CLI error: {data['error']}")
                    return data.get("response") or ""
            except ValueError:
                pass
        return out
