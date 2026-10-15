"""The web UI's HTTP server and REST API (stdlib only).

Long-running work (reviews, comment extraction, gap analysis) runs as
background jobs that the browser polls. Start it with `python papr.py serve`.
The page itself lives in static/."""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import settings
from ..inputs import documents, line_refs, review_letters
from ..inputs.discover import discover
from ..llm import BaseLLM, LLMCancelled, LLMError, canonical_backend, detect_backends, make_llm
from ..project import COMMENT_STATUSES, Project, ProjectNotFound
from ..report import review_report
from ..review import DEPTHS, run_review
from ..revision import chat, gap_analysis, patches, response_letter
from ..revision import checklist, consistency, provenance, submission
from .jobs import JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"

SETTINGS = {"backend": "ollama", "model": None, "ollama_host": None, "num_ctx": None, "timeout": None}
JOBS = JobManager()
_LLM_CACHE: dict[tuple, BaseLLM] = {}
_LLM_LOCK = threading.Lock()
_DETECT_CACHE: dict = {"t": 0.0, "data": None}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def get_llm(project: Project | None = None, override: dict | None = None) -> BaseLLM:
    """The LLM for a project: per-request override > project setting > server default."""
    cfg = dict(SETTINGS)
    if project is not None:
        pl = project.config.get("llm") or {}
        if pl.get("backend"):
            cfg["backend"] = pl["backend"]
            cfg["model"] = pl.get("model")
    if override and override.get("backend"):
        cfg["backend"] = override["backend"]
        cfg["model"] = override.get("model")
    backend = canonical_backend(cfg["backend"])
    key = (backend, cfg.get("model") or None, cfg.get("ollama_host"), cfg.get("num_ctx"), cfg.get("timeout"))
    with _LLM_LOCK:
        if key not in _LLM_CACHE:
            _LLM_CACHE[key] = make_llm(backend, cfg.get("model"), ollama_host=cfg.get("ollama_host"),
                                       num_ctx=cfg.get("num_ctx"), timeout=cfg.get("timeout"))
        return _LLM_CACHE[key]


def backends(refresh: bool = False) -> list[dict]:
    if refresh or not _DETECT_CACHE["data"] or time.time() - _DETECT_CACHE["t"] > 60:
        _DETECT_CACHE["data"] = detect_backends(SETTINGS.get("ollama_host"))
        _DETECT_CACHE["t"] = time.time()
    return _DETECT_CACHE["data"]


def _query(handler) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(handler.path).query).items()}


def _json_body(handler: "Handler") -> dict:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length == 0:
        return {}
    if length > 5_000_000:
        raise ApiError("Request body too large", 413)
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError("Malformed JSON body")
    if not isinstance(data, dict):
        raise ApiError("JSON body must be an object")
    return data


def _project_summary(proj: Project) -> dict:
    return {**proj.config, "gap_plan_text": None, "state_summary": proj.summary()}


def _paths(value) -> list[str]:
    """Accept a list or a newline-separated string of paths."""
    if isinstance(value, str):
        value = value.splitlines()
    return [str(v).strip().strip('"') for v in (value or []) if str(v).strip()]


def _comment_sources(value) -> list[dict]:
    out = []
    for s in value or []:
        if isinstance(s, str):
            s = {"path": s}
        path = str(s.get("path", "")).strip().strip('"')
        if path:
            out.append({"path": path, "label": (s.get("label") or Path(path).name).strip()[:80]})
    labels = set()
    for s in out:   # labels must be unique: they key the comment sources
        base, n = s["label"], 1
        while s["label"] in labels:
            n += 1
            s["label"] = f"{base} ({n})"
        labels.add(s["label"])
    return out


def _optional_path(value, what: str, is_file: bool) -> str | None:
    v = str(value or "").strip().strip('"')
    if not v:
        return None
    if not (Path(v).is_file() if is_file else Path(v).is_dir()):
        raise ApiError(f"{what} not found: {v}")
    return v


def _clean_llm(value) -> dict | None:
    if not isinstance(value, dict) or not value.get("backend"):
        return None
    try:
        backend = canonical_backend(value["backend"])
    except LLMError as e:
        raise ApiError(str(e))
    think = value.get("think")
    return {"backend": backend, "model": (value.get("model") or None),
            "think": think if think in (True, False) else None}


# ---------------------------------------------------------------------------
# Job bodies
# ---------------------------------------------------------------------------

def _job_extract(job, slug: str, labels: list[str] | None = None) -> dict:
    proj = Project.load(slug)
    llm = get_llm(proj)
    sources = [s for s in proj.config["comment_sources"] if labels is None or s["label"] in labels]
    total = 0
    warnings = []
    for i, src in enumerate(sources):
        job.say(f"Reading reviewer comments: {src['label']}", i / max(1, len(sources)))
        try:
            raw = documents.extract_text(src["path"])
            items = review_letters.extract_comments(llm, src["label"], raw, progress=job.say)
            review_letters.replace_source_comments(proj, src["label"], items)
            total += len(items)
            job.say(f"  {len(items)} comment(s) extracted from {src['label']}")
        except LLMCancelled:
            raise
        except (documents.IngestError, LLMError) as e:
            warnings.append(f"{src['label']}: {e}")
            job.say(f"  Could not extract comments from {src['label']}: {e}")
    _annotate_refs(proj, job.say)
    return {"n_comments": total, "warnings": warnings}


def _annotate_refs(proj: Project, say=None) -> None:
    """Attach the text of 'line 123' references (from the submitted PDF)."""
    with proj.mutate():
        n = line_refs.annotate_comments(proj.state["comments"], proj.config.get("submitted_pdf"))
    if n and say:
        say(f"  Resolved line/page references in {n} comment(s) against the submitted PDF")


def _job_review(job, slug: str, options: dict) -> dict:
    proj = Project.load(slug)
    llm = get_llm(proj, override=options.get("llm"))
    job.say(f"Using {llm.display_name}")
    result = run_review(llm, proj, progress=job.say, depth=options.get("depth"),
                               independent=options.get("independent"), force=options.get("force", False))
    proj.reload()
    path = review_report.write_report(proj)
    job.say(f"Report written: {path}")
    overall = result.get("overall") or {}
    return {"report_path": str(path), "recommendation": overall.get("recommendation"),
            "effort_hours_low": overall.get("effort_hours_low"),
            "effort_hours_high": overall.get("effort_hours_high"),
            "effort_confidence": overall.get("effort_confidence")}


def _job_prepare_and_review(job, slug: str, options: dict) -> dict:
    out = {}
    proj = Project.load(slug)
    if proj.config["comment_sources"]:
        out["extraction"] = _job_extract(job, slug)
    if options.get("start_review"):
        out.update(_job_review(job, slug, options))
    return out


def _job_gap(job, slug: str, label: str | None, comment_ids=None, force=False) -> dict:
    proj = Project.load(slug)
    llm = get_llm(proj)
    gap_analysis.run_gap_analysis(llm, proj, manuscript_label=label, progress=job.say,
                                 comment_ids=comment_ids, force=force)
    proj.reload()
    s = proj.summary()
    return {"n_addressed": s["n_addressed"], "errors": proj.state.get("gap_analysis_errors")}


def _job_dict(job) -> dict:
    return job.as_dict() if job else None


# ---------------------------------------------------------------------------
# Route handlers. Each returns a JSON-serialisable object, a ("file", path,
# download_name) tuple, or raises ApiError.
# ---------------------------------------------------------------------------

def h_health(handler, params, body):
    try:
        llm = get_llm()
        desc = llm.describe()
    except LLMError as e:
        desc = {"backend": SETTINGS["backend"], "error": str(e)}
    default = next((b for b in backends() if b["id"] == desc.get("backend")), None)
    return {"ok": True, "default_llm": desc, "default_available": bool(default and default["available"]),
            "settings": {k: v for k, v in SETTINGS.items() if k in ("backend", "model")}}


def h_backends(handler, params, body):
    return {"backends": backends(refresh=_query(handler).get("refresh") == "1"),
            "settings": {k: v for k, v in SETTINGS.items() if k in ("backend", "model")}}


def h_set_settings(handler, params, body):
    if "backend" in body:
        try:
            SETTINGS["backend"] = canonical_backend(body["backend"])
        except LLMError as e:
            raise ApiError(str(e))
        SETTINGS["model"] = body.get("model") or None
    return {"settings": {k: v for k, v in SETTINGS.items() if k in ("backend", "model")}}


def h_get_keys(handler, params, body):
    return {"keys": settings.key_status(), "stored_in": str(settings.keys_path())}


def h_set_key(handler, params, body):
    name = body.get("name")
    if name not in settings.KEYS:
        raise ApiError(f"Unknown key '{name}'")
    settings.save_key(name, body.get("value"))
    _LLM_CACHE.clear()
    backends(refresh=True)
    return {"keys": settings.key_status(), "stored_in": str(settings.keys_path())}


def h_discover(handler, params, body):
    path = _query(handler).get("path", "")
    if not path:
        raise ApiError("path is required")
    try:
        return discover(path)
    except FileNotFoundError as e:
        raise ApiError(str(e), 404)


def h_vent(handler, params, body):
    proj = Project.load(params["slug"])
    comment = proj.get_comment(params["comment_id"])
    if comment is None:
        raise ApiError("Comment not found", 404)
    vent = (body.get("text") or "").strip()
    if not vent:
        raise ApiError("Write your reaction first")
    return response_letter.vent_to_reply(get_llm(proj), proj, comment, vent)


def h_figure_image(handler, params, body):
    proj = Project.load(params["slug"])
    review = proj.state.get("review") or {}
    fig = next((f for f in review.get("figures") or [] if str(f.get("number")) == params["number"]), None)
    if not fig or not fig.get("image"):
        raise ApiError("No image for that figure", 404)
    path = Path(fig["image"]).resolve()
    allowed = [proj.dir.resolve()] + [Path(p).resolve().parent for p in fig.get("resolved") or []]
    if not path.is_file() or not any(path == a or a in path.parents for a in allowed):
        raise ApiError("Image not available", 404)
    return ("inline", path, path.name)


def h_browse(handler, params, body):
    raw_path = _query(handler).get("path", "")
    if not raw_path:
        if os.name == "nt":
            import string
            from ctypes import windll
            bitmask = windll.kernel32.GetLogicalDrives()
            drives = [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if bitmask & (1 << i)]
            return {"cwd": "", "parent": None, "home": str(Path.home()),
                    "entries": [{"name": d, "path": d, "is_dir": True} for d in drives]}
        raw_path = str(Path.home())
    p = Path(raw_path)
    if not p.exists() or not p.is_dir():
        raise ApiError(f"Not a directory: {raw_path}", 404)
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})
            except OSError:
                continue
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else ""
    return {"cwd": str(p), "parent": parent, "home": str(Path.home()), "entries": entries}


def h_list_projects(handler, params, body):
    out = []
    for cfg in Project.list_all():
        try:
            out.append(_project_summary(Project.load(cfg["slug"])))
        except Exception as e:   # one corrupt project must not hide all the others
            out.append({**cfg, "load_error": str(e)})
    return out


def h_create_project(handler, params, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ApiError("'name' is required")
    manuscript_files = body.get("manuscript_files") or []
    if body.get("manuscript_path"):
        mp = str(body["manuscript_path"]).strip().strip('"')
        manuscript_files = [{"path": mp, "label": Path(mp).name}]
    if not manuscript_files:
        raise ApiError("A manuscript file is required")
    mpath = Path(manuscript_files[0]["path"])
    if not mpath.is_file():
        raise ApiError(f"Manuscript file not found: {mpath}")
    sources = _comment_sources(body.get("comment_sources"))
    missing = [s["path"] for s in sources if not Path(s["path"]).is_file()]
    if missing:
        raise ApiError("Reviewer comment file(s) not found: " + "; ".join(missing))
    code_paths, data_paths = _paths(body.get("code_paths")), _paths(body.get("data_paths"))
    standards_paths = _paths(body.get("standards_paths"))
    if any(not Path(p).is_file() for p in standards_paths):
        raise ApiError("Field standards must be existing document files")
    missing = [p for p in code_paths + data_paths if not Path(p).exists()]
    if missing:
        raise ApiError("Code/data path(s) not found: " + "; ".join(missing))
    submitted_pdf = _optional_path(body.get("submitted_pdf"), "Submitted PDF", is_file=True)
    figures_dir = _optional_path(body.get("figures_dir"), "Figures folder", is_file=False)
    llm_cfg = _clean_llm(body.get("llm"))
    rc = body.get("review") or {}
    review_cfg = {"depth": rc.get("depth") if rc.get("depth") in DEPTHS else "thorough",
                  "independent_review": rc.get("independent_review", True) is not False,
                  "personas": rc.get("personas") or None}

    proj = Project.create(
        name=name, journal=body.get("journal", ""), manuscript_files=manuscript_files,
        comment_sources=sources, gap_plan_file=(body.get("gap_plan_file") or None),
        title=body.get("title", ""), field=body.get("field", ""), code_paths=code_paths,
        data_paths=data_paths, standards_paths=standards_paths, llm=llm_cfg or {"backend": None, "model": None, "think": None},
        review=review_cfg, submitted_pdf=submitted_pdf, figures_dir=figures_dir)
    warnings = []
    if proj.config.get("gap_plan_file"):
        try:
            with proj.mutate():
                proj.config["gap_plan_text"] = documents.extract_text(proj.config["gap_plan_file"])
        except Exception as e:
            warnings.append(f"Gap plan file could not be read: {e}")

    job = None
    if sources or body.get("start_review"):
        job = JOBS.start("review" if body.get("start_review") else "extract", proj.slug,
                         "Extract comments and review" if body.get("start_review") else "Extract reviewer comments",
                         lambda j: _job_prepare_and_review(j, proj.slug, {"start_review": body.get("start_review"),
                                                                           "depth": review_cfg["depth"]}))
    result = _project_summary(proj)
    result["warnings"] = warnings
    result["job"] = _job_dict(job)
    return result


def h_get_project(handler, params, body):
    proj = Project.load(params["slug"])
    cfg = dict(proj.config)
    cfg["gap_plan_text"] = (cfg.get("gap_plan_text") or "")[:200] or None
    jobs = [j.as_dict(log_since=10**9) for j in JOBS.for_project(proj.slug)[:5]]
    manifest = provenance.source_manifest(proj)
    return {"config": cfg, "state": proj.state, "summary": proj.summary(), "jobs": jobs,
            "revision": provenance.revision_view(proj, manifest),
            "source_files": [{"path": f["path"], "roles": f["roles"]} for f in manifest["files"]],
            "checklists": {c["id"]: checklist.comment_view(proj, c, manifest) for c in proj.state["comments"]},
            "response_checks": {c["id"]: consistency.check_view(c, manifest) for c in proj.state["comments"]},
            "artifact_versions": {c["id"]: {k: provenance.freshness(c.get(field), manifest) for k, field in
                 (("assessment", "assessment_snapshot_id"), ("gap", "gap_snapshot_id"), ("response_draft", "response_snapshot_id"))}
                 for c in proj.state["comments"]},
            "editable": documents.is_editable(proj.manuscript_path())}


def h_update_project(handler, params, body):
    proj = Project.load(params["slug"])
    new_sources = None
    with proj.mutate():
        cfg = proj.config
        for k in ("name", "title", "journal", "field"):
            if k in body:
                cfg[k] = str(body[k] or "").strip()
        if "manuscript_path" in body:
            mp = str(body["manuscript_path"]).strip().strip('"')
            if not Path(mp).is_file():
                raise ApiError(f"Manuscript file not found: {mp}")
            cfg["manuscript_files"] = [{"path": mp, "label": Path(mp).name}]
        for k in ("code_paths", "data_paths", "standards_paths"):
            if k in body:
                vals = _paths(body[k])
                missing = [p for p in vals if not (Path(p).is_file() if k == "standards_paths" else Path(p).exists())]
                if missing:
                    raise ApiError(f"Path(s) not found: {'; '.join(missing)}")
                cfg[k] = vals
        if "comment_sources" in body:
            srcs = _comment_sources(body["comment_sources"])
            missing = [s["path"] for s in srcs if not Path(s["path"]).is_file()]
            if missing:
                raise ApiError("Reviewer comment file(s) not found: " + "; ".join(missing))
            old_labels = {s["label"] for s in cfg["comment_sources"]}
            new_sources = [s["label"] for s in srcs if s["label"] not in old_labels]
            removed = old_labels - {s["label"] for s in srcs}
            cfg["comment_sources"] = srcs
            if removed:
                proj.state["comments"] = [c for c in proj.state["comments"]
                                          if c.get("origin", "reviewer") != "reviewer" or c.get("source") not in removed]
        if "submitted_pdf" in body:
            cfg["submitted_pdf"] = _optional_path(body["submitted_pdf"], "Submitted PDF", is_file=True)
        if "figures_dir" in body:
            cfg["figures_dir"] = _optional_path(body["figures_dir"], "Figures folder", is_file=False)
        if "llm" in body:
            cfg["llm"] = _clean_llm(body["llm"]) or {"backend": None, "model": None, "think": None}
        if "review" in body and isinstance(body["review"], dict):
            rc = body["review"]
            if rc.get("depth") in DEPTHS:
                cfg["review"]["depth"] = rc["depth"]
            if "independent_review" in rc:
                cfg["review"]["independent_review"] = bool(rc["independent_review"])
            if "personas" in rc:
                cfg["review"]["personas"] = rc["personas"] or None
    if "submitted_pdf" in body:
        _annotate_refs(proj)
    job = None
    if new_sources:
        job = JOBS.start("extract", proj.slug, "Extract reviewer comments",
                         lambda j: _job_extract(j, proj.slug, new_sources))
    return {"config": proj.config, "job": _job_dict(job)}


def h_delete_project(handler, params, body):
    proj = Project.load(params["slug"])
    proj.delete()
    return {"ok": True}


def h_manuscript_text(handler, params, body):
    proj = Project.load(params["slug"])
    path = proj.manuscript_path(_query(handler).get("label"))
    return {"path": str(path), "text": documents.manuscript_review_text(path)}


def h_reextract_comments(handler, params, body):
    proj = Project.load(params["slug"])
    label = body.get("source_label")
    if label and not any(s["label"] == label for s in proj.config["comment_sources"]):
        raise ApiError(f"No comment source labeled '{label}'", 404)
    job = JOBS.start("extract", proj.slug, "Re-extract reviewer comments",
                     lambda j: _job_extract(j, proj.slug, [label] if label else None))
    return {"job": _job_dict(job)}


def h_gap_analysis(handler, params, body):
    proj = Project.load(params["slug"])
    if not proj.state["comments"]:
        raise ApiError("There are no comments to assess yet.")
    label = body.get("manuscript_label")
    ids = body.get("comment_ids")
    if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(cid, str) or not proj.get_comment(cid) for cid in ids)):
        raise ApiError("comment_ids must be a nonempty list of existing comment IDs")
    job = JOBS.start("gap-analysis", proj.slug, "Gap analysis", lambda j: _job_gap(j, proj.slug, label, ids, body.get("force") is True))
    return {"job": _job_dict(job)}


def h_start_review(handler, params, body):
    proj = Project.load(params["slug"])
    opts = {"depth": body.get("depth") if body.get("depth") in DEPTHS else None,
            "independent": body.get("independent") if body.get("independent") in (True, False) else None,
            "force": body.get("force") is True, "llm": _clean_llm(body.get("llm"))}
    try:
        get_llm(proj, override=opts["llm"])
    except LLMError as e:
        raise ApiError(str(e))
    job = JOBS.start("review", proj.slug, "Full review", lambda j: _job_review(j, proj.slug, opts))
    return {"job": _job_dict(job)}


def h_project_jobs(handler, params, body):
    Project.load(params["slug"])
    return {"jobs": [j.as_dict(log_since=10**9) for j in JOBS.for_project(params["slug"])[:20]]}


def h_get_job(handler, params, body):
    job = JOBS.get(params["job_id"])
    if not job:
        raise ApiError("No such job (the server may have restarted)", 404)
    try:
        since = int(_query(handler).get("since", "0"))
    except ValueError:
        since = 0
    return job.as_dict(log_since=since)


def h_cancel_job(handler, params, body):
    return {"cancelled": JOBS.cancel(params["job_id"])}


def h_update_comment(handler, params, body):
    proj = Project.load(params["slug"])
    allowed = {"status", "author_notes", "response_text", "stance"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if "status" in fields and fields["status"] not in COMMENT_STATUSES:
        raise ApiError(f"Invalid status '{fields['status']}'")
    if "stance" in fields:
        stance = fields.pop("stance")
        if stance not in ("address", "address_partially", "clarify", "rebut"):
            raise ApiError(f"Invalid stance '{stance}'")
        with proj.mutate():
            c = proj.get_comment(params["comment_id"])
            if c is None:
                raise ApiError("Comment not found", 404)
            c.setdefault("triage", {})["stance"] = stance
            c["triage"]["stance_set_by_author"] = True
    for k in ("author_notes", "response_text"):
        if k in fields and not isinstance(fields[k], str):
            raise ApiError(f"'{k}' must be a string")
    if fields:
        if "response_text" in fields:
            fields["response_snapshot_id"] = provenance.capture_snapshot(proj, "Author response edit")["id"]
        return proj.update_comment(params["comment_id"], **fields)
    proj.reload()
    return proj.get_comment(params["comment_id"])


def h_delete_comment(handler, params, body):
    proj = Project.load(params["slug"])
    proj.delete_comment(params["comment_id"])
    return {"ok": True}


def h_propose_patch(handler, params, body):
    proj = Project.load(params["slug"])
    comment = proj.get_comment(params["comment_id"])
    if comment is None:
        raise ApiError("Comment not found", 404)
    return patches.propose_patch(get_llm(proj), proj, comment, manuscript_label=body.get("manuscript_label"))


def h_patch_diff(handler, params, body):
    proj = Project.load(params["slug"])
    patch = proj.get_patch(params["patch_id"])
    if patch is None:
        raise ApiError("Patch not found", 404)
    return {"diff": patches.diff_preview(proj, patch)}


def h_apply_patch(handler, params, body):
    return patches.apply_patch(Project.load(params["slug"]), params["patch_id"])


def h_revert_patch(handler, params, body):
    return patches.revert_patch(Project.load(params["slug"]), params["patch_id"])


def h_reject_patch(handler, params, body):
    return patches.reject_patch(Project.load(params["slug"]), params["patch_id"])


def h_revise_patch(handler, params, body):
    proj = Project.load(params["slug"])
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        raise ApiError("Feedback is required")
    return patches.revise_patch(get_llm(proj), proj, params["patch_id"], feedback)


def h_get_chat(handler, params, body):
    proj = Project.load(params["slug"])
    return proj.get_thread(params["thread_id"], create=False)


def h_post_chat(handler, params, body):
    proj = Project.load(params["slug"])
    thread_id = params["thread_id"]
    if not re.match(r"^[A-Za-z0-9._:-]{1,160}$", thread_id):
        raise ApiError("Invalid thread id")
    message = (body.get("message") or "").strip()
    comment_id = body.get("comment_id")
    if comment_id is not None:
        with proj.mutate():
            thread = proj.state["chat_threads"].setdefault(
                thread_id, {"title": thread_id, "comment_id": None, "messages": []})
            thread["comment_id"] = comment_id
    if not message:
        return {"reply": None, "thread": proj.get_thread(thread_id, create=False)}
    reply = chat.project_chat_reply(get_llm(proj), proj, thread_id, message,
                                        manuscript_label=body.get("manuscript_label"))
    proj.reload()
    return {"reply": reply, "thread": proj.get_thread(thread_id, create=False)}


def h_draft_response(handler, params, body):
    proj = Project.load(params["slug"])
    comment = proj.get_comment(params["comment_id"])
    if comment is None:
        raise ApiError("Comment not found", 404)
    return {"response_text": response_letter.draft_response_letter_item(get_llm(proj), proj, comment)}


def h_export_letter(handler, params, body):
    return {"markdown": response_letter.export_response_letter(Project.load(params["slug"]))}


def h_export_letter_docx(handler, params, body):
    proj = Project.load(params["slug"])
    proj.reports_dir.mkdir(parents=True, exist_ok=True)
    out = proj.reports_dir / f"{proj.slug}-response-letter-{time.strftime('%Y%m%d-%H%M%S')}.docx"
    path = response_letter.write_response_letter(proj, out)
    return ("file", path, path.name)


def h_report_md(handler, params, body):
    return {"markdown": review_report.build_report_markdown(Project.load(params["slug"]))}


def h_report_docx(handler, params, body):
    proj = Project.load(params["slug"])
    path = review_report.write_report(proj)
    return ("file", path, path.name)


def h_save_action(handler, params, body):
    proj = Project.load(params["slug"])
    try:
        return checklist.save_action(proj, params["comment_id"], body, params.get("action_id"))
    except ValueError as e:
        raise ApiError(str(e))


def h_delete_action(handler, params, body):
    checklist.delete_action(Project.load(params["slug"]), params["comment_id"], params["action_id"])
    return {"ok": True}


def h_snapshot(handler, params, body):
    return provenance.capture_snapshot(Project.load(params["slug"]), str(body.get("label") or "Author snapshot")[:120])


def h_check_response(handler, params, body):
    proj = Project.load(params["slug"])
    cid = params["comment_id"]
    if not proj.get_comment(cid):
        raise ApiError("Comment not found", 404)
    if not proj.get_comment(cid).get("response_text"):
        raise ApiError("Write or draft a response first")
    job = JOBS.start("response-check", proj.slug, "Check response claims",
                     lambda j: consistency.check_response(get_llm(Project.load(proj.slug)), Project.load(proj.slug),
                                                          cid, force=body.get("force") is True))
    return {"job": _job_dict(job)}


def h_check_submission(handler, params, body):
    proj = Project.load(params["slug"])
    def work(job):
        result = consistency.check_all_responses(get_llm(proj), proj, progress=job.say, force=body.get("force") is True)
        proj.reload()
        review = submission.build_submission_review(proj)
        return {**result, "ready": review["ready"], "blockers": review["blockers"], "warnings": review["warnings"]}
    job = JOBS.start("response-check", proj.slug, "Check submission responses", work)
    return {"job": _job_dict(job)}


def h_submission(handler, params, body):
    proj = Project.load(params["slug"])
    return submission.build_submission_review(proj)


def h_submission_zip(handler, params, body):
    path = submission.write_bundle(Project.load(params["slug"]))
    return ("file", path, path.name)


S = r"(?P<slug>[^/]+)"
ROUTES = [
    ("GET", r"^/api/health$", h_health),
    ("GET", r"^/api/backends$", h_backends),
    ("POST", r"^/api/settings$", h_set_settings),
    ("GET", r"^/api/browse$", h_browse),
    ("GET", r"^/api/keys$", h_get_keys),
    ("POST", r"^/api/keys$", h_set_key),
    ("GET", r"^/api/discover$", h_discover),
    ("GET", r"^/api/jobs/(?P<job_id>[^/]+)$", h_get_job),
    ("POST", r"^/api/jobs/(?P<job_id>[^/]+)/cancel$", h_cancel_job),
    ("GET", r"^/api/projects$", h_list_projects),
    ("POST", r"^/api/projects$", h_create_project),
    ("GET", rf"^/api/projects/{S}$", h_get_project),
    ("POST", rf"^/api/projects/{S}/settings$", h_update_project),
    ("DELETE", rf"^/api/projects/{S}$", h_delete_project),
    ("GET", rf"^/api/projects/{S}/manuscript$", h_manuscript_text),
    ("GET", rf"^/api/projects/{S}/jobs$", h_project_jobs),
    ("POST", rf"^/api/projects/{S}/reextract-comments$", h_reextract_comments),
    ("POST", rf"^/api/projects/{S}/gap-analysis$", h_gap_analysis),
    ("POST", rf"^/api/projects/{S}/review$", h_start_review),
    ("POST", rf"^/api/projects/{S}/snapshots$", h_snapshot),
    ("GET", rf"^/api/projects/{S}/submission$", h_submission),
    ("POST", rf"^/api/projects/{S}/submission/check$", h_check_submission),
    ("GET", rf"^/api/projects/{S}/submission\.zip$", h_submission_zip),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/check-response$", h_check_response),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/actions$", h_save_action),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/actions/(?P<action_id>[^/]+)$", h_save_action),
    ("DELETE", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/actions/(?P<action_id>[^/]+)$", h_delete_action),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)$", h_update_comment),
    ("DELETE", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)$", h_delete_comment),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/propose-patch$", h_propose_patch),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/draft-response$", h_draft_response),
    ("POST", rf"^/api/projects/{S}/comments/(?P<comment_id>[^/]+)/vent$", h_vent),
    ("GET", rf"^/api/projects/{S}/figures/(?P<number>\d+)$", h_figure_image),
    ("GET", rf"^/api/projects/{S}/patches/(?P<patch_id>[^/]+)/diff$", h_patch_diff),
    ("POST", rf"^/api/projects/{S}/patches/(?P<patch_id>[^/]+)/apply$", h_apply_patch),
    ("POST", rf"^/api/projects/{S}/patches/(?P<patch_id>[^/]+)/revert$", h_revert_patch),
    ("POST", rf"^/api/projects/{S}/patches/(?P<patch_id>[^/]+)/reject$", h_reject_patch),
    ("POST", rf"^/api/projects/{S}/patches/(?P<patch_id>[^/]+)/revise$", h_revise_patch),
    ("GET", rf"^/api/projects/{S}/chat/(?P<thread_id>[^/]+)$", h_get_chat),
    ("POST", rf"^/api/projects/{S}/chat/(?P<thread_id>[^/]+)$", h_post_chat),
    ("GET", rf"^/api/projects/{S}/response-letter$", h_export_letter),
    ("GET", rf"^/api/projects/{S}/response-letter\.docx$", h_export_letter_docx),
    ("GET", rf"^/api/projects/{S}/report$", h_report_md),
    ("GET", rf"^/api/projects/{S}/report\.docx$", h_report_docx),
]
COMPILED_ROUTES = [(method, re.compile(pattern), fn) for method, pattern, fn in ROUTES]


class Handler(BaseHTTPRequestHandler):
    server_version = "papr-thinkr/2.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        if "/api/jobs/" in (args[0] if args else "") or "/api/health" in (args[0] if args else ""):
            return   # polling noise
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _host_ok(self) -> bool:
        """Block DNS-rebinding and cross-site requests: this server reads and
        writes local files, so only same-origin localhost pages may use it."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host and host not in ALLOWED_HOSTS and host != self.server.server_address[0]:
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            if (o.hostname or "").lower() not in ALLOWED_HOSTS | {self.server.server_address[0]}:
                return False
        return True

    def _dispatch(self, method: str):
        if not self._host_ok():
            self._discard_body()
            return self._send_json(403, {"error": "Forbidden host/origin"})
        raw_path = urlparse(self.path).path
        if raw_path == "/":
            return self._serve_static("index.html")
        if not raw_path.startswith("/api/"):
            return self._serve_static(unquote(raw_path).lstrip("/"))
        if method in ("POST", "DELETE"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                # forces a CORS preflight for any cross-site request, which we never grant
                self._discard_body()
                return self._send_json(415, {"error": "Content-Type must be application/json"})

        for m, pattern, fn in COMPILED_ROUTES:
            if m != method:
                continue
            match = pattern.match(raw_path)
            if not match:
                continue
            params = {k: unquote(v) for k, v in match.groupdict().items()}
            try:
                body = _json_body(self) if method in ("POST", "PUT", "PATCH") else {}
                result = fn(self, params, body)
                if isinstance(result, tuple) and result and result[0] in ("file", "inline"):
                    self._send_file(result[1], result[2], inline=result[0] == "inline")
                else:
                    self._send_json(200, result)
            except ApiError as e:
                self._send_json(e.status, {"error": str(e)})
            except ProjectNotFound as e:
                self._send_json(404, {"error": str(e)})
            except (patches.PatchError, documents.IngestError) as e:
                self._send_json(400, {"error": str(e)})
            except LLMError as e:
                self._send_json(502, {"error": str(e)})
            except RuntimeError as e:
                self._send_json(409, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._send_json(500, {"error": f"Internal error: {type(e).__name__}: {e}"})
            return
        self._send_json(404, {"error": f"No route for {method} {raw_path}"})

    def _discard_body(self) -> None:
        """Read (and drop) a request body we are refusing, so the client gets
        our error response instead of a connection reset."""
        try:
            n = min(int(self.headers.get("Content-Length", 0) or 0), 5_000_000)
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _serve_static(self, rel_path: str):
        full = (STATIC_DIR / (rel_path or "index.html")).resolve()
        if STATIC_DIR not in full.parents:
            return self._send_json(403, {"error": "Forbidden"})
        if not full.is_file():
            return self._send_json(404, {"error": "Not found"})
        content_type = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        data = full.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, name: str, inline: bool = False):
        data = Path(path).read_bytes()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".docx"):
            ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{name}"')
            self.send_header("X-Saved-Path", str(path).encode("ascii", "replace").decode("ascii"))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _send_json(self, status: int, obj):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # client went away (e.g. page navigated/closed) -- not our problem


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8765, backend: str = "ollama", model: str | None = None,
          ollama_host: str | None = None, num_ctx: int | None = None, timeout: int | None = None) -> None:
    """Start the web UI / REST API and block until Ctrl+C."""
    settings.apply_keys_to_env()
    SETTINGS.update(backend=canonical_backend(backend), model=model, ollama_host=ollama_host,
                    num_ctx=num_ctx, timeout=timeout)
    ALLOWED_HOSTS.add(host)

    print("LLM backends on this machine:")
    for b in backends(refresh=True):
        mark = "OK " if b["available"] else "-- "
        print(f"  {mark}{b['id']:<14} {b['detail']}")
    try:
        print(f"Default: {get_llm().display_name}")
    except LLMError as e:
        print(f"WARNING: default backend unusable: {e}", file=sys.stderr)

    httpd = make_server(host, port)
    print(f"papr-thinkr running at http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.server_close()
