"""LLM layer: JSON handling, schemas, model selection, images per backend."""
from __future__ import annotations

import base64
import json
import unittest

from support import FakeLLM, TempProjects

from papr_thinkr import llm, prompts
from papr_thinkr.llm import base, cloud_apis, ollama

PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class TestJSONHelpers(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(llm.extract_json('[{"a": 1}]'), [{"a": 1}])
        self.assertEqual(llm.extract_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(llm.extract_json('<think>hmm {"no": 1}</think>\n{"a": 3}'), {"a": 3})
        self.assertEqual(llm.extract_json('Sure! Here it is: {"a": [1, 2]} hope that helps'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            llm.extract_json("no json here")

    def test_coerce_to_schema(self):
        arr = {"type": "array", "items": {"type": "object"}}
        self.assertEqual(llm.coerce_to_schema({"items": [{"x": 1}]}, arr), [{"x": 1}])
        self.assertEqual(llm.coerce_to_schema({"x": 1}, arr), [{"x": 1}])
        self.assertEqual(llm.coerce_to_schema([{"x": 1}], {"type": "object"}), {"x": 1})

    def test_strict_and_wrap(self):
        for schema in (prompts.CRITIQUE_SCHEMA, prompts.DECODE_SCHEMA, prompts.TRIAGE_SCHEMA):
            wrapped, was = llm.wrap_array_schema(schema)
            self.assertTrue(was)
            item = llm.strict_schema(wrapped)["properties"]["items"]["items"]
            self.assertEqual(set(item["required"]), set(item["properties"]))
            self.assertFalse(item["additionalProperties"])

    def test_host_and_model_sanitising(self):
        self.assertEqual(ollama.normalize_host("localhost:11434"), "http://127.0.0.1:11434")
        self.assertEqual(ollama.normalize_host("0.0.0.0"), "http://127.0.0.1:11434")
        self.assertIsNone(llm.safe_model_name("auto"))
        with self.assertRaises(llm.LLMError):
            llm.safe_model_name("model; rm -rf /")

    def test_pick_default_local_model(self):
        recs = [{"name": "nomic-embed-text:latest", "capabilities": ["embedding"], "size": 1},
                {"name": "gemma4:31b-cloud", "remote_host": "x", "capabilities": ["completion"], "size": 1},
                {"name": "tiny:1b", "capabilities": ["completion"], "size": 10},
                {"name": "big:70b", "capabilities": ["completion"], "size": 1000}]
        self.assertEqual(llm.OllamaLLM.pick_default(recs), "big:70b")
        recs.append({"name": "qwen3.6:27b", "capabilities": ["completion"], "size": 5})
        self.assertEqual(llm.OllamaLLM.pick_default(recs), "qwen3.6:27b")

    def test_flatten_messages_transcript(self):
        system, prompt = llm.flatten_messages([
            {"role": "system", "content": "S"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}])
        self.assertEqual(system, "S")
        self.assertIn("q1", prompt)
        self.assertTrue(prompt.rstrip().endswith("q2"))

    def test_chat_json_retries_on_garbage(self):
        class Flaky(FakeLLM):
            n = 0

            def _chat(self, messages, temperature, json_schema, think):
                self.n += 1
                return "not json" if self.n == 1 else '{"ok": true}'
        f = Flaky()
        self.assertEqual(f.chat_json([{"role": "user", "content": "x"}]), {"ok": True})
        self.assertEqual(f.n, 2)


class TestImages(TempProjects):
    def png(self):
        p = self.work / "fig.png"
        p.write_bytes(PNG_1PX)
        return str(p)

    def test_non_vision_models_get_text_note_instead(self):
        f = FakeLLM(vision=False)
        f.chat([{"role": "user", "content": "look", "images": [self.png()]}])
        self.assertIsNone(f.calls[-1]["images"])
        self.assertIn("not available to you", f.calls[-1]["prompt"])
        v = FakeLLM(vision=True)
        v.chat([{"role": "user", "content": "look", "images": [self.png()]}])
        self.assertEqual(len(v.calls[-1]["images"]), 1)

    def test_ollama_message_carries_base64(self):
        m = llm.OllamaLLM._message({"role": "user", "content": "x", "images": [self.png(), "missing.png"]})
        self.assertEqual(m["images"], [base64.b64encode(PNG_1PX).decode()])

    def test_openai_message_parts(self):
        msgs = llm.OpenAIAPI._messages([{"role": "user", "content": "x", "images": [self.png()]}])
        self.assertEqual(msgs[0]["content"][1]["type"], "image_url")
        self.assertTrue(msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_anthropic_and_gemini_payloads(self):
        captured = []

        def fake_http(url, payload, headers=None, timeout=600, method=None, retries=3):
            captured.append(payload)
            if "anthropic" in url:
                return {"content": [{"type": "text", "text": "ok"}]}
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        old = cloud_apis.http_json
        cloud_apis.http_json = fake_http
        try:
            a = llm.AnthropicAPI(api_key="k")
            self.assertEqual(a.chat([{"role": "system", "content": "s"},
                                     {"role": "user", "content": "x", "images": [self.png()]}]), "ok")
            blocks = captured[-1]["messages"][0]["content"]
            self.assertEqual(blocks[0]["type"], "image")
            g = llm.GeminiAPI(model="gemini-x", api_key="k")
            self.assertEqual(g.chat([{"role": "user", "content": "x", "images": [self.png()]}]), "ok")
            parts = captured[-1]["contents"][0]["parts"]
            self.assertIn("inline_data", parts[1])
        finally:
            cloud_apis.http_json = old

    def test_usable_images_filters(self):
        self.assertEqual(base.usable_images({"images": [self.png(), str(self.work / "x.svg")]}), [self.png()])


if __name__ == "__main__":
    unittest.main()
