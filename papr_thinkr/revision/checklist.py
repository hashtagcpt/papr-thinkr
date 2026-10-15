"""Author-controlled subrequests with verifiable source and patch links."""
from __future__ import annotations

import copy
import uuid
from pathlib import Path

from ..inputs.documents import extract_text, read_text_file
from ..project import ProjectNotFound
from .provenance import digest, file_hash, source_manifest


def suggested_actions(comment: dict) -> list[dict]:
    if "revision_actions" in comment:
        return copy.deepcopy(comment["revision_actions"])
    triage, decoded = comment.get("triage") or {}, comment.get("decoded") or {}
    detailed = (comment.get("critique") or {}).get("actions") or []
    texts = [a["action"] for a in detailed] or triage.get("required_changes") or decoded.get("what_would_satisfy") or []
    if not texts:
        texts = [comment.get("suggested_action") or "Address this request or document the reason for rebuttal."]
    return [{"id": digest([comment["id"], i, text])[:12], "text": text, "done": False,
             "evidence": [], "patch_ids": [], "rationale": "", "response_excerpt": "",
             "validation": detailed[i].get("validation", "") if i < len(detailed) else ""}
            for i, text in enumerate(texts)]


def source_text(path: str) -> str:
    if Path(path).suffix.lower() in (".docx", ".pdf", ".odt", ".rtf", ".doc"):
        return extract_text(path)
    return read_text_file(Path(path))[0]


def evidence_view(evidence: dict, manifest: dict, texts: dict) -> dict:
    row = dict(evidence)
    path = str(Path(row.get("path") or "").resolve())
    source = next((f for f in manifest["files"] if f["path"] == path), None)
    row.update(path=path, verified=False, freshness="unknown")
    if not source or not source.get("sha256"):
        row["reason"] = "Evidence source is missing or is no longer part of this project."
        return row
    row["freshness"] = "current" if row.get("sha256") == source["sha256"] else "stale"
    try:
        if path not in texts:
            texts[path] = source_text(path)
        text = texts[path]
    except (OSError, RuntimeError, ValueError) as e:
        row["reason"] = f"Cannot read evidence: {e}"
        return row
    quote = row.get("quote") or ""
    idx = text.find(quote) if quote else -1
    row["verified"] = idx >= 0
    if idx >= 0:
        row["line"] = text.count("\n", 0, idx) + 1
    else:
        row["reason"] = "Quoted evidence is absent from the current source."
    return row


def comment_view(project, comment, manifest=None, texts=None) -> dict:
    manifest = manifest or source_manifest(project)
    texts = texts if texts is not None else {}
    patches = {p["id"]: p for p in project.state["patches"]}
    actions = suggested_actions(comment)
    for action in actions:
        action["evidence"] = [evidence_view(e, manifest, texts) for e in action.get("evidence") or []]
        action["patches"] = []
        for pid in action.get("patch_ids") or []:
            p = patches.get(pid)
            linked = {"id": pid, "status": p.get("status") if p else "missing", "verified": False}
            if p and p.get("status") == "applied" and p.get("replace"):
                row = evidence_view({"path": p["file"], "quote": p["replace"],
                                     "sha256": p.get("applied_sha256")}, manifest, texts)
                linked.update(verified=row["verified"], location=p["file"], line=row.get("line"))
            elif p and p.get("status") == "applied" and p.get("replace") == "":
                # Deletions are supported only while the exact post-edit version remains.
                source = next((f for f in manifest["files"] if f["path"] == str(Path(p["file"]).resolve())), {})
                linked["verified"] = bool(p.get("applied_sha256") and
                                           p["applied_sha256"] == source.get("sha256"))
            action["patches"].append(linked)
        ev = action["evidence"]
        patch_links = action["patches"]
        supported = bool(ev or patch_links) and all(e["verified"] and e["freshness"] == "current" for e in ev) \
            and all(p["verified"] for p in patch_links)
        rebuttal = bool(action.get("rationale", "").strip()) and (
            comment.get("status") == "wontfix" or (comment.get("triage") or {}).get("stance") == "rebut")
        excerpt = action.get("response_excerpt") or ""
        action["response_link_valid"] = bool(excerpt and excerpt in (comment.get("response_text") or ""))
        action["complete"] = bool(action.get("done") and (supported or rebuttal))
    return {"comment_id": comment["id"], "actions": actions,
            "complete": bool(actions) and all(a["complete"] for a in actions),
            "completed": sum(a["complete"] for a in actions), "total": len(actions),
            "response_text": comment.get("response_text") or ""}


def save_action(project, comment_id: str, fields: dict, action_id: str | None = None) -> dict:
    allowed = {"text", "done", "evidence", "patch_ids", "rationale", "response_excerpt"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    for key in ("text", "rationale", "response_excerpt"):
        if key in fields and not isinstance(fields[key], str):
            raise ValueError(f"{key} must be a string")
    if "done" in fields and not isinstance(fields["done"], bool):
        raise ValueError("done must be true or false")
    if "text" in fields and not fields["text"].strip():
        raise ValueError("An action needs a description")
    if "evidence" in fields:
        if not isinstance(fields["evidence"], list) or len(fields["evidence"]) > 30:
            raise ValueError("evidence must be a list of at most 30 source quotes")
        manifest = source_manifest(project)
        normalized = []
        for e in fields["evidence"]:
            if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not isinstance(e.get("quote"), str):
                raise ValueError("Each evidence link needs a file path and an exact quote")
            if not e["quote"].strip():
                raise ValueError("Evidence needs a nonempty quote")
            path = str(Path(e["path"]).resolve())
            row = next((f for f in manifest["files"] if f["path"] == path), None)
            if not row or not row.get("sha256"):
                raise ValueError("Choose an existing manuscript, code, data, or other configured project source")
            if e["quote"] not in source_text(path):
                raise ValueError("The evidence quote was not found in that file")
            if file_hash(path) != row["sha256"]:
                raise ValueError("The evidence file changed while it was being read; try again")
            normalized.append({"id": e.get("id") or uuid.uuid4().hex[:12], "path": path,
                               "quote": e["quote"], "location": str(e.get("location") or ""),
                               "sha256": row["sha256"], "snapshot_id": manifest["id"]})
        fields["evidence"] = normalized
    with project.mutate():
        comment = project.get_comment(comment_id)
        if comment is None:
            raise ProjectNotFound(f"No comment with id {comment_id}")
        actions = suggested_actions(comment)
        if "patch_ids" in fields:
            ids = fields["patch_ids"]
            if not isinstance(ids, list) or any(not isinstance(pid, str) or pid not in comment.get("patch_ids", []) for pid in ids):
                raise ValueError("Patch links must belong to this comment")
        if action_id is None:
            if not fields.get("text", "").strip():
                raise ValueError("An action needs a description")
            action = {"id": uuid.uuid4().hex[:12], "text": "", "done": False, "evidence": [],
                      "patch_ids": [], "rationale": "", "response_excerpt": ""}
            actions.append(action)
        else:
            action = next((a for a in actions if a["id"] == action_id), None)
            if action is None:
                raise ProjectNotFound("No such revision action")
        action.update(fields)
        comment["revision_actions"] = actions
        return copy.deepcopy(action)


def delete_action(project, comment_id, action_id):
    with project.mutate():
        c = project.get_comment(comment_id)
        if c is None:
            raise ProjectNotFound("Comment not found")
        actions = suggested_actions(c)
        if not any(a["id"] == action_id for a in actions):
            raise ProjectNotFound("Revision action not found")
        c["revision_actions"] = [a for a in actions if a["id"] != action_id]


def response_input_hash(comment: dict) -> str:
    return digest({k: comment.get(k) for k in ("comment_text", "response_text", "revision_actions", "patch_ids", "status", "triage")})
