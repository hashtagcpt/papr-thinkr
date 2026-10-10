"""The HTTP API end to end (fake LLM), API-key storage, and the server's
security checks."""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
import unittest
from urllib.parse import quote

from support import FakeLLM, TempProjects

from papr_thinkr import settings
from papr_thinkr.web import server


class TestSettings(TempProjects):
    def test_keys_roundtrip_and_env_precedence(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        settings.save_key("ANTHROPIC_API_KEY", "sk-ant-1234567890abcdef")
        self.assertTrue(settings.keys_path().is_file())
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-1234567890abcdef")
        st = {k["name"]: k for k in settings.key_status()}
        self.assertEqual(st["ANTHROPIC_API_KEY"]["masked"], "sk-a…cdef")
        settings.save_key("ANTHROPIC_API_KEY", "")
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        os.environ["OPENAI_API_KEY"] = "from-env"
        try:
            settings.save_key("OPENAI_API_KEY", "from-file-xxxxxxxxx")
            settings._from_file.discard("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = "from-env"
            settings.apply_keys_to_env()
            self.assertEqual(os.environ["OPENAI_API_KEY"], "from-env")      # the user's own variable wins
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            settings.save_key("OPENAI_API_KEY", "")
        with self.assertRaises(ValueError):
            settings.save_key("SOMETHING_ELSE", "x")


class TestServer(TempProjects):
    def setUp(self):
        super().setUp()
        self.fake = FakeLLM(vision=True)
        self._orig_get_llm = server.get_llm
        server.get_llm = lambda project=None, override=None: self.fake
        self.httpd = server.make_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.get_llm = self._orig_get_llm
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        hdrs = {"Content-Type": "application/json"} if method != "GET" else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=json.dumps(body or {}) if method != "GET" else None, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(data) if "json" in ctype else data), ctype

    def wait_job(self, job_id, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            _s, j, _c = self.req("GET", f"/api/jobs/{job_id}")
            if j["status"] in ("done", "error", "cancelled"):
                return j
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_folder_to_report(self):
        demo = self.copy_demo()
        status, found, _ = self.req("GET", f"/api/discover?path={quote(str(demo))}")
        self.assertEqual(status, 200)
        status, res, _ = self.req("POST", "/api/projects", {
            "name": "API demo", "manuscript_path": found["manuscript"],
            "comment_sources": [{"path": p, "label": "Round #1 / letter"} for p in found["reviews"]],
            "code_paths": found["code_paths"], "data_paths": "\n".join(found["data_paths"]),
            "submitted_pdf": found["submitted_pdf"], "review": {"depth": "quick"}, "start_review": True})
        self.assertEqual(status, 200, res)
        job = self.wait_job(res["job"]["id"])
        self.assertEqual(job["status"], "done", job.get("error"))
        slug = res["slug"]
        _s, proj, _c = self.req("GET", f"/api/projects/{slug}")
        self.assertTrue(proj["state"]["review"]["figures"])
        rev = [c for c in proj["state"]["comments"] if c["origin"] == "reviewer"]
        cid = rev[1]["id"]
        status, c, _ = self.req("POST", f"/api/projects/{slug}/comments/{quote(cid, safe='')}",
                                {"author_notes": "n1", "stance": "clarify"})
        self.assertEqual((status, c["triage"]["stance"]), (200, "clarify"))
        status, _r, _c = self.req("POST", f"/api/projects/{slug}/comments/{quote(cid, safe='')}", {"status": "bogus"})
        self.assertEqual(status, 400)
        status, v, _ = self.req("POST", f"/api/projects/{slug}/comments/{quote(cid, safe='')}/vent",
                                {"text": "They didn't read it!"})
        self.assertEqual(status, 200)
        self.assertIn("Section 2.4", v["response_text"])
        status, img, ctype = self.req("GET", f"/api/projects/{slug}/figures/2")
        self.assertEqual((status, ctype, img[:4]), (200, "image/png", b"\x89PNG"))
        status, _r, _c = self.req("GET", f"/api/projects/{slug}/figures/3")
        self.assertEqual(status, 404)                         # figure 3's file is missing
        status, data, _ = self.req("GET", f"/api/projects/{slug}/report.docx")
        self.assertEqual((status, data[:2]), (200, b"PK"))
        status, patch, _ = self.req("POST", f"/api/projects/{slug}/comments/{quote(cid, safe='')}/propose-patch", {})
        self.assertTrue(patch["validated"], patch.get("validation_message"))
        status, _p, _c = self.req("POST", f"/api/projects/{slug}/patches/{patch['id']}/apply", {})
        self.assertEqual(status, 200)
        self.assertIn("Interocular differences", (demo / "manuscript" / "main.tex").read_text(encoding="utf-8"))

    def test_keys_endpoints_never_return_secrets(self):
        os.environ.pop("GEMINI_API_KEY", None)
        status, res, _ = self.req("POST", "/api/keys", {"name": "GEMINI_API_KEY", "value": "AIzaSyTESTTESTTEST1234"})
        self.assertEqual(status, 200)
        gem = next(k for k in res["keys"] if k["name"] == "GEMINI_API_KEY")
        self.assertTrue(gem["set"])
        self.assertNotIn("TESTTEST", json.dumps(res))
        self.req("POST", "/api/keys", {"name": "GEMINI_API_KEY", "value": ""})
        status, _r, _c = self.req("POST", "/api/keys", {"name": "PATH", "value": "x"})
        self.assertEqual(status, 400)

    def test_security_checks(self):
        status, _r, _c = self.req("GET", "/api/projects", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        status, _r, _c = self.req("POST", "/api/projects", {"name": "x"}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _r, _c = self.req("POST", "/api/keys", {"name": "GEMINI_API_KEY", "value": "x"},
                                  headers={"Origin": "https://evil.example.com"})
        self.assertEqual(status, 403)
        status, _r, _c = self.req("GET", "/api/projects/..%5C..%5Cwindows")
        self.assertEqual(status, 404)
        status, _r, _c = self.req("GET", "/..%2F..%2Fpapr.py")
        self.assertIn(status, (403, 404))
        status, res, _ = self.req("POST", "/api/projects", {"name": "x", "manuscript_path": str(self.work / "nope.md")})
        self.assertEqual(status, 400)
        self.assertIn("not found", res["error"])


if __name__ == "__main__":
    unittest.main()
