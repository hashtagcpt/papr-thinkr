"""Project workspace: holds config (which real files this project points at)
and mutable state (comments, chat threads, patch history, review results) as
plain JSON on disk. No database -- every project is just a folder you can
read, diff, or delete by hand.

Concurrency: the server is multi-threaded and some operations (a full review,
gap analysis) run for many minutes. All writes go through Project.mutate(),
which holds a per-project lock, re-reads the latest state from disk, lets the
caller change it, and saves atomically -- so a long job finishing never
clobbers notes or statuses the author saved in the meantime."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .inputs.documents import manuscript_review_text

PROJECTS_ROOT = Path(os.environ.get("PAPR_PROJECTS_DIR") or
                     Path(__file__).resolve().parent.parent / "projects")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")

COMMENT_STATUSES = ("unaddressed", "partially_addressed", "addressed", "unclear", "wontfix")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(slug: str) -> threading.RLock:
    with _LOCKS_GUARD:
        if slug not in _LOCKS:
            _LOCKS[slug] = threading.RLock()
        return _LOCKS[slug]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60].strip("-")
    return slug or "project"


def _atomic_write_json(path: Path, obj) -> None:
    data = json.dumps(obj, indent=2, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(data, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: another process (AV scanner, editor) briefly holds the file
            time.sleep(0.1 * (attempt + 1))
    os.replace(tmp, path)


class ProjectNotFound(RuntimeError):
    pass


def default_config(name: str, slug: str) -> dict:
    return {
        "name": name,
        "slug": slug,
        "journal": "",
        "title": "",
        "field": "",
        "created_at": time.time(),
        "manuscript_files": [],   # [{"path": str, "label": str}]
        "comment_sources": [],    # [{"path": str, "label": str}]
        "code_paths": [],         # files or directories
        "data_paths": [],         # files or directories
        "submitted_pdf": None,    # the line-numbered PDF the reviewers read
        "figures_dir": None,      # figure files submitted separately (optional)
        "gap_plan_file": None,
        "gap_plan_text": None,
        "llm": {"backend": None, "model": None, "think": None},   # None -> server default
        "review": {"depth": "thorough", "independent_review": True, "personas": None},
    }


def default_state() -> dict:
    return {
        "comments": [],
        "chat_threads": {},   # thread_id -> {"title", "comment_id", "messages": [...]}
        "patches": [],        # [{id, file, find, replace, rationale, comment_id, status, ...}]
        "gap_analysis_generated_at": None,
        "review": None,       # results of the last full review (see review.py)
    }


class Project:
    def __init__(self, slug: str):
        if not SLUG_RE.match(slug or ""):
            raise ProjectNotFound(f"Invalid project id '{slug}'")
        self.slug = slug
        self.dir = PROJECTS_ROOT / slug
        self.backups_dir = self.dir / "backups"
        self.reports_dir = self.dir / "reports"
        self.config_path = self.dir / "project.json"
        self.state_path = self.dir / "state.json"
        self.config: dict = {}
        self.state: dict = {}
        self.lock = _lock_for(slug)

    # ---------- lifecycle ----------

    @classmethod
    def create(cls, name: str, journal: str = "", manuscript_files: list[dict] | None = None,
               comment_sources: list[dict] | None = None, gap_plan_file: str | None = None,
               **extra) -> "Project":
        PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
        base_slug = slugify(name)
        with _LOCKS_GUARD:
            slug = base_slug
            n = 1
            while (PROJECTS_ROOT / slug).exists():
                n += 1
                slug = f"{base_slug}-{n}"
            (PROJECTS_ROOT / slug).mkdir(parents=True)
        proj = cls(slug)
        proj.backups_dir.mkdir(exist_ok=True)
        proj.config = default_config(name, slug)
        proj.config.update({
            "journal": journal or "",
            "manuscript_files": manuscript_files or [],
            "comment_sources": comment_sources or [],
            "gap_plan_file": gap_plan_file,
        })
        for k, v in extra.items():
            if k in proj.config and v is not None:
                proj.config[k] = v
        proj.state = default_state()
        proj.save()
        return proj

    @classmethod
    def load(cls, slug: str) -> "Project":
        proj = cls(slug)
        if not proj.config_path.exists():
            raise ProjectNotFound(f"No project named '{slug}'")
        proj.reload()
        return proj

    def reload(self) -> None:
        with self.lock:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            base = default_config(cfg.get("name", self.slug), self.slug)
            base.update(cfg)
            for k in ("llm", "review"):
                merged = default_config("", "")[k]
                merged.update(cfg.get(k) or {})
                base[k] = merged
            self.config = base
            state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
            full = default_state()
            full.update(state)
            for c in full["comments"]:
                c.setdefault("patch_ids", [])
                c.setdefault("status", "unaddressed")
                c.setdefault("author_notes", "")
                c.setdefault("response_text", "")
                c.setdefault("origin", "reviewer")
            self.state = full

    @staticmethod
    def list_all() -> list[dict]:
        PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
        out = []
        for d in sorted(PROJECTS_ROOT.iterdir()):
            cfg_path = d / "project.json"
            if cfg_path.exists():
                try:
                    out.append(json.loads(cfg_path.read_text(encoding="utf-8")))
                except Exception:
                    continue
        return out

    def save(self) -> None:
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.config_path, self.config)
            _atomic_write_json(self.state_path, self.state)

    @contextmanager
    def mutate(self):
        """Lock, reload the latest on-disk state, let the caller modify
        self.config / self.state, then save atomically."""
        with self.lock:
            self.reload()
            yield self
            self.save()

    def delete(self) -> None:
        with self.lock:
            shutil.rmtree(self.dir, ignore_errors=True)

    # ---------- derived ----------

    @property
    def mode(self) -> str:
        """'revision' when reviewer comments exist, else 'first-draft'."""
        has_reviewer = any(c.get("origin", "reviewer") == "reviewer" for c in self.state.get("comments", []))
        return "revision" if (self.config.get("comment_sources") or has_reviewer) else "first-draft"

    def summary(self) -> dict:
        comments = self.state.get("comments", [])
        reviewer = [c for c in comments if c.get("origin", "reviewer") == "reviewer"]
        ai = [c for c in comments if c.get("origin") == "ai-review"]
        review = self.state.get("review") or {}
        return {
            "n_comments": len(comments),
            "n_reviewer_comments": len(reviewer),
            "n_ai_items": len(ai),
            "n_addressed": sum(1 for c in comments if c.get("status") == "addressed"),
            "n_patches": len(self.state.get("patches", [])),
            "gap_analysis_generated_at": self.state.get("gap_analysis_generated_at"),
            "review_generated_at": review.get("generated_at"),
            "mode": self.mode,
        }

    # ---------- manuscript access ----------

    def manuscript_path(self, label: str | None = None) -> Path:
        files = self.config.get("manuscript_files") or []
        if not files:
            raise ProjectNotFound("Project has no manuscript file configured")
        if label is None:
            return Path(files[0]["path"])
        for f in files:
            if f.get("label") == label:
                return Path(f["path"])
        raise ProjectNotFound(f"No manuscript file labeled '{label}'")

    def manuscript_text(self, label: str | None = None) -> tuple[str, str]:
        """(text for review/analysis, file extension). Works for .docx/.pdf too."""
        path = self.manuscript_path(label)
        return manuscript_review_text(path), path.suffix.lower()

    def backup_file(self, path: Path) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backups_dir / f"{ts}-{uuid.uuid4().hex[:6]}__{path.name}"
        shutil.copy2(path, dest)
        return dest

    # ---------- comments ----------

    def get_comment(self, comment_id: str) -> dict | None:
        for c in self.state["comments"]:
            if c["id"] == comment_id:
                return c
        return None

    def update_comment(self, comment_id: str, **fields) -> dict:
        with self.mutate():
            c = self.get_comment(comment_id)
            if c is None:
                raise ProjectNotFound(f"No comment with id {comment_id}")
            c.update(fields)
            return dict(c)

    def delete_comment(self, comment_id: str) -> None:
        with self.mutate():
            before = len(self.state["comments"])
            self.state["comments"] = [c for c in self.state["comments"] if c["id"] != comment_id]
            if len(self.state["comments"]) == before:
                raise ProjectNotFound(f"No comment with id {comment_id}")

    # ---------- chat ----------

    def get_thread(self, thread_id: str, create: bool = True) -> dict:
        threads = self.state["chat_threads"]
        if thread_id not in threads:
            empty = {"title": thread_id, "comment_id": None, "messages": []}
            if not create:
                return empty
            with self.mutate():
                self.state["chat_threads"].setdefault(thread_id, empty)
        return self.state["chat_threads"][thread_id]

    def append_message(self, thread_id: str, role: str, content: str) -> None:
        with self.mutate():
            thread = self.state["chat_threads"].setdefault(
                thread_id, {"title": thread_id, "comment_id": None, "messages": []})
            thread["messages"].append({"role": role, "content": content, "ts": time.time()})

    # ---------- patches ----------

    def add_patch(self, file: str, find: str | None, replace: str | None,
                  rationale: str, comment_id: str | None, **extra) -> dict:
        patch = {
            "id": uuid.uuid4().hex[:8],
            "file": file,
            "find": find,
            "replace": replace,
            "rationale": rationale,
            "comment_id": comment_id,
            "status": "proposed",  # proposed | applied | rejected | reverted
            "created_at": time.time(),
            "backup_path": None,
            **extra,
        }
        with self.mutate():
            self.state["patches"].append(patch)
            if comment_id:
                c = self.get_comment(comment_id)
                if c is not None:
                    c.setdefault("patch_ids", []).append(patch["id"])
        return patch

    def get_patch(self, patch_id: str) -> dict | None:
        for p in self.state["patches"]:
            if p["id"] == patch_id:
                return p
        return None
