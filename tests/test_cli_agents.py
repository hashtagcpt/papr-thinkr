"""Claude Code / Codex / Gemini backends, exercised against fake CLIs
(npm-style .cmd shims on Windows, shell scripts elsewhere): argument handling,
stdin prompts, schema files, images, auth detection, timeouts, cancellation."""
from __future__ import annotations

import base64
import json
import os
import sys
import textwrap
import threading
import unittest

from support import TempProjects

from papr_thinkr import llm

FAKE = textwrap.dedent(r'''
    import json, os, sys
    args = sys.argv[1:]
    kind = args[0]
    args = args[1:]
    if "--help" in args:
        print("--ephemeral --color --output-schema --output-last-message --output-format --image")
        sys.exit(0)
    prompt = sys.stdin.read()
    log = {"args": args, "prompt_len": len(prompt), "has_marker": "MARKER-123" in prompt,
           "cwd_files": sorted(os.listdir(".")), "env_trust": os.environ.get("GEMINI_CLI_TRUST_WORKSPACE")}
    with open(sys.argv[0] + ".log", "a", encoding="utf-8") as f:
        f.write(json.dumps(log) + "\n")
    if kind == "codex":
        out = args[args.index("--output-last-message") + 1]
        schema_ok = False
        if "--output-schema" in args:
            schema = json.load(open(args[args.index("--output-schema") + 1], encoding="utf-8"))
            schema_ok = schema.get("additionalProperties") is False
        reply = json.dumps({"items": [{"ok": schema_ok}]}) if "--output-schema" in args else "plain answer"
        open(out, "w", encoding="utf-8").write(reply)
        print("some progress chatter on stdout")
    elif kind == "gemini":
        print("Loaded cached credentials.")
        print(json.dumps({"response": "```json\n[{\"ok\": true}]\n```", "stats": {}}))
    elif kind == "claude":
        if "--json-schema" in args or "JSON Schema" in prompt:
            print(json.dumps({"type": "result", "is_error": False, "result": "",
                              "structured_output": {"items": [{"ok": True}]}}))
        elif "fail" in prompt:
            print(json.dumps({"type": "result", "is_error": True, "result": "quota exceeded"}))
        else:
            print(json.dumps({"type": "result", "is_error": False, "result": "hello from claude"}))
    elif kind == "slow":
        import time; time.sleep(30)
''')
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
ARRAY = {"type": "array", "items": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}


class TestCLIAgents(TempProjects):
    def make_cli(self, kind: str) -> str:
        script = self.work / "fakecli.py"
        script.write_text(FAKE, encoding="utf-8")
        if os.name == "nt":
            shim = self.work / f"{kind}.cmd"
            shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" {kind} %*\r\n', encoding="utf-8")
        else:
            shim = self.work / kind
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {kind} "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        return str(shim)

    def log(self):
        p = self.work / "fakecli.py.log"
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]

    def test_codex_schema_sandbox_and_images(self):
        img = self.work / "figure1.png"
        img.write_bytes(PNG_1PX)
        b = llm.CodexCLI(executable=self.make_cli("codex"), model="gpt-5")
        out = b.chat_json([{"role": "system", "content": "sys"},
                           {"role": "user", "content": "MARKER-123 go", "images": [str(img)]}], json_schema=ARRAY)
        self.assertEqual(out, [{"ok": True}])
        entry = self.log()[-1]
        self.assertTrue(entry["has_marker"])
        for flag in ("exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-m", "-", "--image"):
            self.assertIn(flag, entry["args"])
        self.assertEqual(b.chat([{"role": "user", "content": "hi"}]), "plain answer")

    def test_gemini_json_and_trusted_scratch_dir(self):
        b = llm.GeminiCLI(executable=self.make_cli("gemini"))
        self.assertEqual(b.chat_json([{"role": "user", "content": "MARKER-123"}], json_schema=ARRAY), [{"ok": True}])
        entry = self.log()[-1]
        self.assertTrue(entry["has_marker"])
        self.assertEqual(entry["env_trust"], "true")
        self.assertEqual(entry["cwd_files"], [])          # runs in an empty scratch directory

    def test_gemini_auth_detection(self):
        keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")
        saved = {k: os.environ.pop(k, None) for k in keys}
        old_home = os.environ.get("USERPROFILE"), os.environ.get("HOME")
        os.environ["USERPROFILE"] = os.environ["HOME"] = str(self.work)
        try:
            self.assertIn("API key", llm.GeminiCLI.auth_problem())
            os.environ["GEMINI_API_KEY"] = "x"
            self.assertIsNone(llm.GeminiCLI.auth_problem())
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            for k, v in zip(("USERPROFILE", "HOME"), old_home):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_claude_code_tools_off_unless_images(self):
        b = llm.ClaudeCodeCLI(executable=self.make_cli("claude"), model="sonnet")
        self.assertEqual(b.chat_json([{"role": "user", "content": "MARKER-123"}], json_schema=ARRAY), [{"ok": True}])
        entry = self.log()[-1]
        i = entry["args"].index("--tools")
        self.assertEqual(entry["args"][i + 1], "")
        if os.name == "nt":       # through a .cmd shim the schema goes in the prompt, not argv
            self.assertNotIn("--json-schema", entry["args"])
        self.assertEqual(b.chat([{"role": "user", "content": "hi"}]), "hello from claude")
        with self.assertRaises(llm.LLMError):
            b.chat([{"role": "user", "content": "please fail"}])
        img = self.work / "fig2.png"
        img.write_bytes(PNG_1PX)
        b.chat([{"role": "user", "content": "look", "images": [str(img)]}])
        entry = self.log()[-1]
        i = entry["args"].index("--tools")
        self.assertEqual(entry["args"][i + 1], "Read")
        self.assertEqual(entry["cwd_files"], ["fig2.png"])   # only the figure is visible to it

    def test_timeout_and_cancel(self):
        b = llm.CodexCLI(executable=self.make_cli("slow"), timeout=3)
        b._help_cache[b.executable + " exec --help"] = ""
        with self.assertRaises(llm.LLMError):
            b.chat([{"role": "user", "content": "x"}])
        ctx = llm.CallContext(threading.Event())
        ctx.cancel_event.set()
        llm.use_context(ctx)
        try:
            with self.assertRaises(llm.LLMCancelled):
                b.chat([{"role": "user", "content": "x"}])
        finally:
            llm.use_context(None)


if __name__ == "__main__":
    unittest.main()
