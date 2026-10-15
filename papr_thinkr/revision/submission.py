"""Submission checklist and a reproducible response/change-summary bundle."""
from __future__ import annotations

import io
import json
import re
import time
import tempfile
import zipfile
from pathlib import Path

from ..report.word import markdown_to_docx
from .checklist import comment_view, source_text
from .consistency import COMPLETION_CLAIM, check_view
from .provenance import capture_snapshot, revision_view, source_manifest
from .response_letter import export_response_letter


_PLACEHOLDER = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|\?\?|\[(?:insert|add|citation|page|line|ref)[^\]]*\]", re.I)


def build_submission_review(project, manifest=None):
    manifest = manifest or source_manifest(project)
    revision = revision_view(project, manifest)
    issues, checklist, texts = [], [], {}

    def issue(code, message, cid=None, level="blocker", action_id=None):
        issues.append({"code": code, "level": level, "comment_id": cid, "action_id": action_id, "message": message})

    for f in manifest["files"]:
        if f.get("error"):
            issue("missing_source", f"Cannot read {f['path']}: {f['error']}")
        elif "manuscript" in f["roles"]:
            try:
                texts[f["path"]] = source_text(f["path"])
                if _PLACEHOLDER.search(texts[f["path"]]):
                    issue("manuscript_placeholder", f"Unresolved placeholder in manuscript source: {f['path']}")
            except (OSError, RuntimeError, ValueError):
                issue("unreadable_manuscript", f"Cannot extract manuscript text: {f['path']}")
    comments = [c for c in project.state["comments"] if c.get("origin", "reviewer") == "reviewer"]
    if not comments:
        issue("no_reviewer_comments", "No reviewer requests are loaded. Add or extract the decision letter before preparing a response.")
    for c in comments:
        cid = c["id"]
        view = comment_view(project, c, manifest, texts)
        audit = check_view(c, manifest)
        checklist.append({"comment_id": cid, "reviewer": c.get("reviewer"), "request": c["comment_text"],
                          "status": c.get("status"), "actions": view["actions"], "complete": view["complete"],
                          "response_text": c.get("response_text") or "", "response_check": audit})
        if c.get("status") not in ("addressed", "wontfix"):
            issue("open_request", "Reviewer request is still open.", cid)
        if not view["complete"]:
            issue("incomplete_actions", f"{view['completed']}/{view['total']} actions have completion evidence or a rebuttal rationale.", cid)
        response = (c.get("response_text") or "").strip()
        if not response:
            issue("missing_response", "Response has not been written.", cid)
        elif _PLACEHOLDER.search(response):
            issue("response_placeholder", "Response contains a placeholder that needs to be filled in.", cid)
        if response:
            if audit["freshness"] != "current":
                issue("response_audit_needed", f"Response audit is {audit['freshness']}; check it against the current sources and checklist.", cid)
            elif not audit["ok"]:
                issue("response_inconsistent", "The response audit found unsupported or unclear claims, or unanswered subrequests.", cid)
            if COMPLETION_CLAIM.search(response) and not any(a["complete"] for a in view["actions"]):
                issue("unsupported_completion", "The response claims completed changes but no action has current completion evidence.", cid)
            if COMPLETION_CLAIM.search(response) and not any(a["patches"] or any(
                    e["verified"] and any(f["path"] == e["path"] and "manuscript" in f["roles"] for f in manifest["files"])
                    for e in a["evidence"]) for a in view["actions"]):
                issue("missing_manuscript_location", "The response describes a change without linking to a manuscript passage or edit.", cid, level="warning")
        for a in view["actions"]:
            if a.get("response_excerpt") and not a["response_link_valid"]:
                issue("broken_response_link", "The linked response passage is no longer present.", cid, action_id=a["id"])
            for p in a["patches"]:
                if not p["verified"]:
                    issue("patch_not_current", f"Linked edit {p['id']} is {p['status']} or its replacement is no longer in the manuscript.", cid, action_id=a["id"])
            for e in a["evidence"]:
                if not e["verified"] or e["freshness"] != "current":
                    issue("stale_evidence", "An evidence link needs to be checked and saved against the current source.", cid, action_id=a["id"])
    for c in project.state["comments"]:
        if c.get("origin") == "ai-review" and c.get("status") not in ("addressed", "wontfix"):
            issue("open_ai_finding", f"Unresolved {c.get('severity', '')} independent finding: {c['comment_text'][:180]}",
                  c["id"], "blocker" if c.get("severity") == "critical" else "warning")
    if revision["review"] == "stale":
        issue("stale_review", "The independent review predates current file versions. Rerun it to refresh affected calls.", level="warning")
    if revision["gap_analysis"] == "stale":
        issue("stale_gap_analysis", "The gap analysis predates current file versions.", level="warning")
    blockers = sum(i["level"] == "blocker" for i in issues)
    return {"generated_at": time.time(), "snapshot_id": manifest["id"], "ready": blockers == 0,
            "blockers": blockers, "warnings": sum(i["level"] == "warning" for i in issues),
            "issues": issues, "checklist": checklist, "revision": revision,
            "note": "Readiness reflects recorded evidence and model assessments; authors make the submission decision."}


def review_markdown(review):
    lines = ["# Submission review", "", "**Ready for author review**" if review["ready"] else "**DRAFT — unresolved items remain**",
             "", f"{review['blockers']} blockers; {review['warnings']} warnings.", "",
             f"Source snapshot: `{review['snapshot_id']}`", "", review["note"], ""]
    for issue in review["issues"]:
        lines.append(f"- **{issue['level']}** {issue.get('comment_id') or ''}: {issue['message']}")
    for c in review["checklist"]:
        lines += ["", f"## {c['reviewer']} — {c['comment_id']}", "", c["request"], ""]
        for a in c["actions"]:
            lines.append(f"- [{'x' if a['complete'] else ' '}] {a['text']}")
            for e in a["evidence"]:
                lines.append(f"  - Evidence: {e['path']} ({e.get('location') or 'line ' + str(e.get('line', '?'))}): {e['quote']}")
        for claim in c["response_check"].get("claims") or []:
            lines.append(f"- Response claim ({claim['verdict']}): {claim['response_quote']} — {claim['reason']}")
    return "\n".join(lines) + "\n"


def change_summary(project, review):
    lines = ["# Manuscript change summary", "", f"Source snapshot: `{review['snapshot_id']}`", ""]
    for c in review["checklist"]:
        lines += [f"## {c['reviewer']} — {c['comment_id']}", "", c["request"], ""]
        for a in c["actions"]:
            lines.append(f"- {'Complete' if a['complete'] else 'Open'}: {a['text']}")
            if a.get("rationale"):
                lines.append(f"  Rationale: {a['rationale']}")
            for e in a["evidence"]:
                lines.append(f"  Evidence ({e['freshness']}): {e['path']} — {e.get('location') or 'line ' + str(e.get('line', '?'))}\n\n> {e['quote']}\n")
    lines += ["## Edit history", ""]
    for p in project.state["patches"]:
        if p["status"] not in ("applied", "reverted"):
            continue
        lines += [f"- {p['id']} ({p['status']}) — {p.get('comment_id') or 'unlinked'} — {p['file']}",
                  f"  {p.get('rationale') or ''}", f"  Before: {p.get('find') or '(empty)'}",
                  f"  After: {p.get('replace') or '(deleted)'}", ""]
    return "\n".join(lines)


def write_bundle(project, out_path=None) -> Path:
    snapshot = capture_snapshot(project, "Submission bundle")
    project.reload()
    review = build_submission_review(project, snapshot)
    # Do not export author notes, chat/vent text, credentials, backups or raw source files.
    public_manifest = {k: snapshot[k] for k in ("id", "files", "created_at", "label")}
    response = export_response_letter(project)
    summary = change_summary(project, review)
    out = Path(out_path) if out_path else project.reports_dir / f"{project.slug}-submission-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    contents = {
        "response-letter.md": response,
        "change-summary.md": summary,
        "submission-review.md": review_markdown(review),
        "submission-review.json": json.dumps(review, indent=2, ensure_ascii=False),
        "checklist.json": json.dumps(review["checklist"], indent=2, ensure_ascii=False),
        "source-manifest.json": json.dumps(public_manifest, indent=2, ensure_ascii=False),
        "README.txt": ("READY FOR AUTHOR REVIEW\n" if review["ready"] else "DRAFT: UNRESOLVED ITEMS REMAIN\n") +
                      "Read submission-review.md before using this bundle. Source files are not included.\n"}
    # Build in memory so a writer failure cannot leave a misleading partial ZIP.
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="papr-submission-") as temp:
        word_path = markdown_to_docx(response, Path(temp) / "response-letter.docx")
        word_bytes = word_path.read_bytes()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        archive.writestr("response-letter.docx", word_bytes)
    if source_manifest(project)["id"] != snapshot["id"]:
        raise RuntimeError("Source files changed while assembling the bundle. Export again.")
    out.write_bytes(buffer.getvalue())
    return out
