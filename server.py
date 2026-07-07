#!/usr/bin/env python3
"""papr-thinkr: a local, offline manuscript-revision co-author.

Runs a small HTTP server (stdlib only) that serves a browser UI and a REST
API backed by a local Ollama model. Run with:

    python server.py [--port 8765] [--model gemma3:27b-it-qat]

Then open http://localhost:8765 in a browser.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from copilot import analysis, comments as comments_mod, ingest, patches
from copilot.llm import OllamaClient, LLMError, DEFAULT_MODEL
from copilot.project import Project, ProjectNotFound

WEB_DIR = Path(__file__).resolve().parent / "web"

LLM: OllamaClient  # set in main()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _json_body(handler: "Handler") -> dict:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise ApiError("Malformed JSON body")


def _project_summary(proj: Project) -> dict:
    return {**proj.config, "state_summary": {
        "n_comments": len(proj.state["comments"]),
        "n_addressed": sum(1 for c in proj.state["comments"] if c.get("status") == "addressed"),
        "n_patches": len(proj.state["patches"]),
        "gap_analysis_generated_at": proj.state.get("gap_analysis_generated_at"),
    }}


# ---------------------------------------------------------------------------
# Route handlers. Each returns a JSON-serializable object, or raises ApiError.
# ---------------------------------------------------------------------------

def h_health(handler, params, body):
    return {
        "ollama_available": LLM.is_available(),
        "models": LLM.list_models(),
        "default_model": LLM.model,
    }


def h_browse(handler, params, body):
    qs = parse_qs(urlparse(handler.path).query)
    raw_path = qs.get("path", [""])[0]
    if not raw_path:
        if os.name == "nt":
            import string
            from ctypes import windll
            drives = []
            bitmask = windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if bitmask & (1 << i):
                    drives.append(f"{letter}:\\")
            return {"cwd": "", "parent": None, "entries": [
                {"name": d, "path": d, "is_dir": True} for d in drives
            ]}
        raw_path = str(Path.home())

    p = Path(raw_path)
    if not p.exists() or not p.is_dir():
        raise ApiError(f"Not a directory: {raw_path}", 404)

    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if child.name.startswith("."):
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
    except PermissionError:
        pass

    parent = str(p.parent) if p.parent != p else None
    return {"cwd": str(p), "parent": parent, "entries": entries}


def h_list_projects(handler, params, body):
    return [Project.load(cfg["slug"]).config | {"state_summary": _project_summary(Project.load(cfg["slug"]))["state_summary"]}
            for cfg in Project.list_all()]


def h_create_project(handler, params, body):
    name = body.get("name")
    if not name:
        raise ApiError("'name' is required")
    proj = Project.create(
        name=name,
        journal=body.get("journal", ""),
        manuscript_files=body.get("manuscript_files", []),
        comment_sources=body.get("comment_sources", []),
        gap_plan_file=body.get("gap_plan_file"),
    )
    proj.config["title"] = body.get("title", "")
    proj.config["model"] = body.get("model")
    warnings = []
    if proj.config.get("gap_plan_file"):
        try:
            proj.config["gap_plan_text"] = ingest.extract_text(proj.config["gap_plan_file"])
        except Exception as e:
            proj.config["gap_plan_text"] = None
            warnings.append(f"Gap plan file could not be read: {e}")
    proj.save()

    if not Path(proj.manuscript_path()).exists():
        warnings.append(f"Manuscript file not found: {proj.manuscript_path()}")

    # auto-ingest comment sources on creation
    for source in proj.config["comment_sources"]:
        try:
            raw = ingest.extract_text(source["path"])
            items = comments_mod.extract_comments(LLM, source["label"], raw)
            proj.set_comments(items, source["label"])
        except Exception as e:
            traceback.print_exc()
            warnings.append(f"Could not extract comments from '{source['label']}' ({source['path']}): {e}")

    result = _project_summary(proj)
    result["warnings"] = warnings
    return result


def h_get_project(handler, params, body):
    proj = Project.load(params["slug"])
    return {"config": proj.config, "state": proj.state}


def h_delete_project(handler, params, body):
    proj = Project.load(params["slug"])
    proj.delete()
    return {"ok": True}


def h_manuscript_text(handler, params, body):
    proj = Project.load(params["slug"])
    qs = parse_qs(urlparse(handler.path).query)
    label = qs.get("label", [None])[0]
    path = proj.manuscript_path(label)
    return {"path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}


def h_reextract_comments(handler, params, body):
    proj = Project.load(params["slug"])
    source_label = body.get("source_label")
    source = next((s for s in proj.config["comment_sources"] if s["label"] == source_label), None)
    if not source:
        raise ApiError(f"No comment source labeled '{source_label}'", 404)
    raw = ingest.extract_text(source["path"])
    items = comments_mod.extract_comments(LLM, source_label, raw)
    proj.set_comments(items, source_label)
    return {"comments": proj.state["comments"]}


def h_gap_analysis(handler, params, body):
    proj = Project.load(params["slug"])
    label = body.get("manuscript_label")
    updated = analysis.run_gap_analysis(LLM, proj, manuscript_label=label)
    return {"comments": updated}


def h_update_comment(handler, params, body):
    proj = Project.load(params["slug"])
    allowed = {"status", "author_notes", "response_text"}
    fields = {k: v for k, v in body.items() if k in allowed}
    c = proj.update_comment(params["comment_id"], **fields)
    return c


def h_delete_comment(handler, params, body):
    proj = Project.load(params["slug"])
    proj.delete_comment(params["comment_id"])
    return {"ok": True}


def h_propose_patch(handler, params, body):
    proj = Project.load(params["slug"])
    comment = proj.get_comment(params["comment_id"])
    if comment is None:
        raise ApiError("Comment not found", 404)
    patch = patches.propose_patch(LLM, proj, comment, manuscript_label=body.get("manuscript_label"))
    return patch


def h_patch_diff(handler, params, body):
    proj = Project.load(params["slug"])
    patch = proj.get_patch(params["patch_id"])
    if patch is None:
        raise ApiError("Patch not found", 404)
    return {"diff": patches.diff_preview(proj, patch)}


def h_apply_patch(handler, params, body):
    proj = Project.load(params["slug"])
    patch = patches.apply_patch(proj, params["patch_id"])
    return patch


def h_reject_patch(handler, params, body):
    proj = Project.load(params["slug"])
    patch = patches.reject_patch(proj, params["patch_id"])
    return patch


def h_revise_patch(handler, params, body):
    proj = Project.load(params["slug"])
    feedback = body.get("feedback", "")
    patch = patches.revise_patch(LLM, proj, params["patch_id"], feedback)
    return patch


def h_get_chat(handler, params, body):
    proj = Project.load(params["slug"])
    return proj.get_thread(params["thread_id"])


def h_post_chat(handler, params, body):
    proj = Project.load(params["slug"])
    thread_id = params["thread_id"]
    message = body.get("message", "")
    comment_id = body.get("comment_id")
    if comment_id is not None:
        thread = proj.get_thread(thread_id)
        thread["comment_id"] = comment_id
        proj.save()
    reply = analysis.project_chat_reply(LLM, proj, thread_id, message,
                                         manuscript_label=body.get("manuscript_label"))
    return {"reply": reply, "thread": proj.get_thread(thread_id)}


def h_draft_response(handler, params, body):
    proj = Project.load(params["slug"])
    comment = proj.get_comment(params["comment_id"])
    if comment is None:
        raise ApiError("Comment not found", 404)
    text = analysis.draft_response_letter_item(LLM, proj, comment)
    return {"response_text": text}


def h_export_letter(handler, params, body):
    proj = Project.load(params["slug"])
    return {"markdown": analysis.export_response_letter(proj)}


ROUTES = [
    ("GET", r"^/api/health$", h_health),
    ("GET", r"^/api/browse$", h_browse),
    ("GET", r"^/api/projects$", h_list_projects),
    ("POST", r"^/api/projects$", h_create_project),
    ("GET", r"^/api/projects/(?P<slug>[^/]+)$", h_get_project),
    ("DELETE", r"^/api/projects/(?P<slug>[^/]+)$", h_delete_project),
    ("GET", r"^/api/projects/(?P<slug>[^/]+)/manuscript$", h_manuscript_text),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/reextract-comments$", h_reextract_comments),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/gap-analysis$", h_gap_analysis),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/comments/(?P<comment_id>[^/]+)$", h_update_comment),
    ("DELETE", r"^/api/projects/(?P<slug>[^/]+)/comments/(?P<comment_id>[^/]+)$", h_delete_comment),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/comments/(?P<comment_id>[^/]+)/propose-patch$", h_propose_patch),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/comments/(?P<comment_id>[^/]+)/draft-response$", h_draft_response),
    ("GET", r"^/api/projects/(?P<slug>[^/]+)/patches/(?P<patch_id>[^/]+)/diff$", h_patch_diff),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/patches/(?P<patch_id>[^/]+)/apply$", h_apply_patch),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/patches/(?P<patch_id>[^/]+)/reject$", h_reject_patch),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/patches/(?P<patch_id>[^/]+)/revise$", h_revise_patch),
    ("GET", r"^/api/projects/(?P<slug>[^/]+)/chat/(?P<thread_id>[^/]+)$", h_get_chat),
    ("POST", r"^/api/projects/(?P<slug>[^/]+)/chat/(?P<thread_id>[^/]+)$", h_post_chat),
    ("GET", r"^/api/projects/(?P<slug>[^/]+)/response-letter$", h_export_letter),
]

COMPILED_ROUTES = [(method, re.compile(pattern), fn) for method, pattern, fn in ROUTES]


class Handler(BaseHTTPRequestHandler):
    server_version = "papr-thinkr/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/" :
            return self._serve_static("index.html")
        if not path.startswith("/api/") :
            return self._serve_static(path.lstrip("/"))

        for m, pattern, fn in COMPILED_ROUTES:
            if m != method:
                continue
            match = pattern.match(path)
            if match:
                try:
                    body = _json_body(self) if method in ("POST", "PUT", "PATCH") else {}
                    result = fn(self, match.groupdict(), body)
                    self._send_json(200, result)
                except ApiError as e:
                    self._send_json(e.status, {"error": str(e)})
                except (ProjectNotFound, patches.PatchError, ingest.IngestError) as e:
                    self._send_json(400, {"error": str(e)})
                except LLMError as e:
                    self._send_json(502, {"error": str(e)})
                except Exception as e:
                    traceback.print_exc()
                    self._send_json(500, {"error": f"Internal error: {e}"})
                return
        self._send_json(404, {"error": f"No route for {method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _serve_static(self, rel_path: str):
        if not rel_path:
            rel_path = "index.html"
        full = (WEB_DIR / rel_path).resolve()
        if WEB_DIR not in full.parents and full != WEB_DIR:
            self._send_json(403, {"error": "Forbidden"})
            return
        if not full.exists() or not full.is_file():
            self._send_json(404, {"error": "Not found"})
            return
        content_type = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        data = full.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # client went away (e.g. page navigated/closed) -- not our problem


def main():
    global LLM
    parser = argparse.ArgumentParser(description="papr-thinkr local server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    args = parser.parse_args()

    LLM = OllamaClient(host=args.ollama_host, model=args.model)

    if not LLM.is_available():
        print(f"WARNING: could not reach Ollama at {args.ollama_host}. "
              f"Start it with 'ollama serve' -- the UI will still load but "
              f"LLM features will fail until it's reachable.", file=sys.stderr)
    elif args.model not in LLM.list_models():
        print(f"WARNING: model '{args.model}' not found in Ollama. "
              f"Pull it with: ollama pull {args.model}", file=sys.stderr)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"papr-thinkr running at http://{args.host}:{args.port}")
    print(f"Using Ollama model: {args.model} @ {args.ollama_host}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
