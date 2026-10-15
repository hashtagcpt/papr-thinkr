"""Content versions for source files and immutable manuscript snapshots.

Only manuscripts are copied. Code, data and figures are streamed through
SHA-256 and recorded in the manifest, so large datasets are not duplicated.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from ..inputs.code_and_data import SKIP_DIRS
from ..inputs.documents import (_TEX_INCLUDE_RE, _strip_tex_comments, _tex_include_path,
                                read_text_file, tex_component_files)
from ..project import _atomic_write_json


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_manifest(project) -> dict:
    paths: dict[str, set[str]] = {}
    runtime_dir = project.dir.resolve()

    def add(path, role):
        if not path:
            return
        p = Path(path).expanduser().resolve()
        if p.is_dir():
            # Walk explicitly to avoid following directory links and generated files.
            stack = [p]
            while stack:
                folder = stack.pop()
                try:
                    children = sorted(folder.iterdir())
                except OSError:
                    paths.setdefault(str(folder), set()).add(role)
                    continue
                for child in children:
                    if child.is_symlink() or child.name.startswith(".") or child.name in SKIP_DIRS or child.resolve() == runtime_dir:
                        continue
                    if child.is_dir():
                        stack.append(child)
                    else:
                        paths.setdefault(str(child.resolve()), set()).add(role)
        else:
            paths.setdefault(str(p), set()).add(role)

    cfg = project.config
    for source in cfg.get("manuscript_files") or []:
        add(source["path"], "manuscript")
        p = Path(source["path"])
        if p.suffix.lower() == ".tex" and p.is_file():
            for component in tex_component_files(p):
                add(component, "manuscript")
        # Include separately stored figures referenced by text manuscripts.
        if p.suffix.lower() in (".tex", ".md", ".txt") and p.is_file():
            sources = tex_component_files(p) if p.suffix.lower() == ".tex" else [p]
            roots = [p.resolve().parent]
            for component in sources:
                for gp in re.findall(r"\\graphicspath\s*\{((?:\{[^}]*\})+)\}", read_text_file(component)[0]):
                    roots.extend(p.resolve().parent / name for name in re.findall(r"\{([^}]*)\}", gp))
            for component in sources:
                text = _strip_tex_comments(read_text_file(component)[0])
                # Track bibliography changes and includes that are currently missing, too.
                for match in _TEX_INCLUDE_RE.finditer(text):
                    add(_tex_include_path(component.parent, match.group(2) or match.group(3), p.resolve().parent), "manuscript")
                for match in re.finditer(r"\\(bibliography|addbibresource)(?:\[[^\]]*\])?\{([^}]+)\}", text):
                    for name in match.group(2).split(","):
                        target = component.parent / name.strip()
                        if not target.suffix:
                            target = target.with_suffix(".bib")
                        if not target.is_file() and (p.resolve().parent / target.name).is_file():
                            target = p.resolve().parent / target.name
                        add(target, "manuscript")
                targets = re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}|!\[[^\]]*\]\(([^)]+)\)", text)
                for pair in targets:
                    name = pair[0] or pair[1]
                    if re.match(r"https?://|data:", name):
                        continue
                    candidates = [root / name for root in [component.parent] + roots]
                    choices = [candidate for c in candidates for candidate in
                               ([c] if c.suffix else [c.with_suffix(ext) for ext in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".tif", ".tiff")])]
                    target = next((c for c in choices if c.is_file()), candidates[0])
                    add(target, "figure")
    for source in cfg.get("comment_sources") or []:
        add(source["path"], "reviews")
    for key, role in (("code_paths", "code"), ("data_paths", "data"), ("standards_paths", "standard")):
        for path in cfg.get(key) or []:
            add(path, role)
    for key, role in (("submitted_pdf", "submitted"), ("figures_dir", "figure"), ("gap_plan_file", "plan")):
        add(cfg.get(key), role)
    files = []
    for path, roles in sorted(paths.items()):
        row = {"path": path, "roles": sorted(roles)}
        try:
            row.update(sha256=file_hash(path), size=Path(path).stat().st_size)
        except OSError as e:
            row.update(sha256=None, error=type(e).__name__)
        files.append(row)
    # Also invalidate conclusions when the target venue or revision context changes.
    context = {k: cfg.get(k) for k in ("journal", "title", "field", "gap_plan_text")}
    return {"id": digest({"files": files, "context": context}), "files": files, "context": context}


def scope_signature(manifest: dict, roles=None) -> str:
    files = manifest["files"] if roles is None else [f for f in manifest["files"] if set(f["roles"]) & set(roles)]
    return digest({"files": files, "context": manifest["context"]})


def capture_snapshot(project, label="Revision", manifest=None) -> dict:
    manifest = manifest or source_manifest(project)
    folder = project.dir / "snapshots" / manifest["id"]
    with project.lock:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "manifest.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
        else:
            saved = {**manifest, "created_at": time.time(), "label": label, "manuscript_copies": []}
            for i, row in enumerate(manifest["files"]):
                if "manuscript" not in row["roles"] or not row.get("sha256"):
                    continue
                data = Path(row["path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise RuntimeError("A manuscript changed while taking a snapshot. Try again.")
                dest = folder / f"{i:04d}-{Path(row['path']).name}"
                dest.write_bytes(data)
                saved["manuscript_copies"].append({"source": row["path"], "copy": dest.name})
            _atomic_write_json(path, saved)
        with project.mutate():
            snapshots = project.state.setdefault("revision_snapshots", [])
            if not any(s["id"] == saved["id"] for s in snapshots):
                snapshots.append({k: saved[k] for k in ("id", "created_at", "label")})
    return saved


def freshness(snapshot_id, current: dict) -> str:
    if not snapshot_id:
        return "unknown"
    return "current" if snapshot_id == current["id"] else "stale"


def revision_view(project, current=None) -> dict:
    current = current or source_manifest(project)
    review = project.state.get("review") or {}
    previous = None
    snapshots = project.state.get("revision_snapshots") or []
    if snapshots:
        path = project.dir / "snapshots" / snapshots[-1]["id"] / "manifest.json"
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
    before = {f["path"]: f for f in (previous or {}).get("files", [])}
    after = {f["path"]: f for f in current["files"]}
    changed = [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)] if previous else []
    return {"current_id": current["id"], "snapshots": snapshots, "changed_files": changed,
            "source_errors": [f for f in current["files"] if f.get("error")],
            "review": freshness(review.get("snapshot_id"), current),
            "gap_analysis": freshness(project.state.get("gap_analysis_snapshot_id"), current)}
