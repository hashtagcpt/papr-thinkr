"""Project workspace: holds config (which real files this project points at)
and mutable state (comments, chat threads, patch history, gap analysis) as
plain JSON on disk. No database -- every project is just a folder you can
read, diff, or delete by hand."""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

PROJECTS_ROOT = Path(__file__).resolve().parent.parent / "projects"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


class ProjectNotFound(RuntimeError):
    pass


class Project:
    def __init__(self, slug: str):
        self.slug = slug
        self.dir = PROJECTS_ROOT / slug
        self.backups_dir = self.dir / "backups"
        self.config_path = self.dir / "project.json"
        self.state_path = self.dir / "state.json"
        self.config: dict = {}
        self.state: dict = {}

    # ---------- lifecycle ----------

    @classmethod
    def create(cls, name: str, journal: str, manuscript_files: list[dict],
               comment_sources: list[dict], gap_plan_file: str | None = None) -> "Project":
        base_slug = slugify(name)
        slug = base_slug
        n = 1
        while (PROJECTS_ROOT / slug).exists():
            n += 1
            slug = f"{base_slug}-{n}"

        proj = cls(slug)
        proj.dir.mkdir(parents=True, exist_ok=True)
        proj.backups_dir.mkdir(exist_ok=True)
        proj.config = {
            "name": name,
            "slug": slug,
            "journal": journal,
            "title": "",
            "created_at": time.time(),
            "manuscript_files": manuscript_files,  # [{"path": str, "label": str}]
            "comment_sources": comment_sources,      # [{"path": str, "label": str}]
            "gap_plan_file": gap_plan_file,
            "gap_plan_text": None,
            "model": None,
        }
        proj.state = {
            "comments": [],
            "chat_threads": {},   # thread_id -> {"title": str, "comment_id": str|None, "messages": [...]}
            "patches": [],        # [{id, file, find, replace, rationale, comment_id, status, created_at}]
            "gap_analysis_generated_at": None,
        }
        proj.save()
        return proj

    @classmethod
    def load(cls, slug: str) -> "Project":
        proj = cls(slug)
        if not proj.config_path.exists():
            raise ProjectNotFound(f"No project named '{slug}'")
        proj.config = json.loads(proj.config_path.read_text(encoding="utf-8"))
        proj.state = json.loads(proj.state_path.read_text(encoding="utf-8")) if proj.state_path.exists() else {}
        proj.state.setdefault("comments", [])
        proj.state.setdefault("chat_threads", {})
        proj.state.setdefault("patches", [])
        proj.state.setdefault("gap_analysis_generated_at", None)
        return proj

    @staticmethod
    def list_all() -> list[dict]:
        PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
        out = []
        for d in sorted(PROJECTS_ROOT.iterdir()):
            cfg_path = d / "project.json"
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    out.append(cfg)
                except Exception:
                    continue
        return out

    def save(self) -> None:
        self.config_path.write_text(json.dumps(self.config, indent=2), encoding="utf-8")
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def delete(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------- manuscript access ----------

    def manuscript_path(self, label: str | None = None) -> Path:
        files = self.config["manuscript_files"]
        if not files:
            raise RuntimeError("Project has no manuscript files")
        if label is None:
            return Path(files[0]["path"])
        for f in files:
            if f["label"] == label:
                return Path(f["path"])
        raise RuntimeError(f"No manuscript file labeled '{label}'")

    def backup_file(self, path: Path) -> Path:
        self.backups_dir.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backups_dir / f"{ts}__{path.name}"
        shutil.copy2(path, dest)
        return dest

    # ---------- comments ----------

    def set_comments(self, comments: list[dict], source_label: str) -> None:
        # replace any existing comments from this source, keep others
        existing = [c for c in self.state["comments"] if c["source"] != source_label]
        self.state["comments"] = existing + comments
        self.save()

    def get_comment(self, comment_id: str) -> dict | None:
        for c in self.state["comments"]:
            if c["id"] == comment_id:
                return c
        return None

    def update_comment(self, comment_id: str, **fields) -> dict:
        c = self.get_comment(comment_id)
        if c is None:
            raise RuntimeError(f"No comment with id {comment_id}")
        c.update(fields)
        self.save()
        return c

    def delete_comment(self, comment_id: str) -> None:
        before = len(self.state["comments"])
        self.state["comments"] = [c for c in self.state["comments"] if c["id"] != comment_id]
        if len(self.state["comments"]) == before:
            raise RuntimeError(f"No comment with id {comment_id}")
        self.save()

    # ---------- chat ----------

    def get_thread(self, thread_id: str) -> dict:
        threads = self.state["chat_threads"]
        if thread_id not in threads:
            threads[thread_id] = {"title": thread_id, "comment_id": None, "messages": []}
            self.save()
        return threads[thread_id]

    def append_message(self, thread_id: str, role: str, content: str) -> None:
        thread = self.get_thread(thread_id)
        thread["messages"].append({"role": role, "content": content, "ts": time.time()})
        self.save()

    # ---------- patches ----------

    def add_patch(self, file: str, find: str | None, replace: str | None,
                  rationale: str, comment_id: str | None) -> dict:
        patch = {
            "id": str(uuid.uuid4())[:8],
            "file": file,
            "find": find,
            "replace": replace,
            "rationale": rationale,
            "comment_id": comment_id,
            "status": "proposed",  # proposed | applied | rejected
            "created_at": time.time(),
            "backup_path": None,
        }
        self.state["patches"].append(patch)
        self.save()
        return patch

    def get_patch(self, patch_id: str) -> dict | None:
        for p in self.state["patches"]:
            if p["id"] == patch_id:
                return p
        return None
