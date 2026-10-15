"""The point-by-point response letter: drafting one response with the model,
and assembling the whole letter (Markdown or .docx)."""
from __future__ import annotations

from pathlib import Path

from .. import prompts
from ..inputs.chunking import chunk_manuscript, find_chunk_for_query
from ..inputs.line_refs import refs_block
from ..llm import BaseLLM
from ..project import Project
from ..report.word import markdown_to_docx
from .provenance import capture_snapshot


def _decoded_notes(comment: dict) -> str:
    d = comment.get("decoded") or {}
    if not d:
        return ""
    out = f"plain reading: {d.get('plain_language', '')}"
    if d.get("misunderstanding", "none") != "none":
        out += (f"; the reviewer appears to have misunderstood ({d['misunderstanding']}): "
                f"{d.get('misunderstanding_explanation', '')} Manuscript text: \"{d.get('manuscript_evidence', '')}\""
                f"; suggested clarifying edit: {d.get('clarifying_edit') or 'none'}")
    if d.get("clarity") != "clear" and d.get("clarification_request"):
        out += f"; the comment is ambiguous -- state the interpretation used: {d['clarification_request']}"
    return out


def draft_response_letter_item(llm: BaseLLM, project: Project, comment: dict) -> str:
    snapshot = capture_snapshot(project, "Response draft")
    patches = [project.get_patch(pid) for pid in comment.get("patch_ids", [])]
    applied = [p for p in patches if p and p.get("status") == "applied"]
    if applied:
        change_summary = "\n".join(
            f"- Rationale: {p.get('rationale', '')}\n  Replaced: \"{p.get('find') or ''}\"\n  With: \"{p.get('replace') or ''}\""
            for p in applied)
    else:
        change_summary = "(no manuscript edit applied yet)"
    t = comment.get("triage") or {}
    triage_notes = "(none)"
    if t:
        triage_notes = (f"assessment: reviewer {t.get('agreement')}; rationale: {t.get('rationale', '')}; "
                        f"required changes: {'; '.join(t.get('required_changes') or []) or 'none'}; "
                        f"evidence in manuscript: {t.get('evidence') or 'none'}; "
                        f"draft rebuttal: {t.get('rebuttal') or 'none'}")
    decoded = _decoded_notes(comment)
    if decoded:
        triage_notes = (triage_notes if t else "") + ("; " if t else "") + decoded
    for group in comment.get("joint_guidance") or []:
        outline = next((r["response_outline"] for r in group.get("responses") or [] if r["comment_id"] == comment["id"]), "")
        triage_notes += (f"\nJoint revision plan for {', '.join(group['comment_ids'])}: {group['approach']}. "
                         f"Planned changes (not evidence of completion): {'; '.join(group['manuscript_changes'])}. "
                         f"Response outline for THIS reviewer: {outline}. Do not claim a planned change was made unless applied evidence confirms it.")

    prompt = prompts.RESPONSE_LETTER_ITEM.format(
        reviewer=comment["reviewer"], comment_text=comment["comment_text"],
        status=comment.get("status", "unaddressed"), stance=t.get("stance", "address"),
        change_summary=change_summary, triage_notes=triage_notes,
        author_notes=comment.get("author_notes") or "(none)")
    result = llm.chat_json([
        {"role": "system", "content": "You draft formal peer-review response letters and respond only in JSON."},
        {"role": "user", "content": prompt},
    ], json_schema=prompts.RESPONSE_LETTER_ITEM_SCHEMA)
    text = (result.get("response_text", "") if isinstance(result, dict) else "").strip()
    if text:
        project.update_comment(comment["id"], response_text=text, response_snapshot_id=snapshot["id"])
    return text


def export_response_letter(project: Project) -> str:
    title = project.config.get("title") or project.config.get("name")
    lines = ["# Point-by-Point Response to Reviewers", "", f"**Manuscript:** {title}", ""]
    journal = project.config.get("journal")
    if journal:
        lines += [f"**Journal:** {journal}", ""]
    by_reviewer: dict[str, list[dict]] = {}
    for c in project.state["comments"]:
        if c.get("origin", "reviewer") != "reviewer":
            continue
        by_reviewer.setdefault(c["reviewer"], []).append(c)
    if not by_reviewer:
        lines.append("_No reviewer comments in this project._")
    for reviewer, items in by_reviewer.items():
        lines.append(f"\n## {reviewer}\n")
        for i, c in enumerate(sorted(items, key=lambda x: x.get("order", 0)), 1):
            quoted = "\n".join("> " + ln for ln in c["comment_text"].splitlines())
            lines.append(f"**Comment {i}.**\n\n{quoted}\n")
            resp = c.get("response_text")
            if not resp:
                t = c.get("triage") or {}
                if t.get("rebuttal") and t.get("stance") in ("rebut", "clarify"):
                    resp = t["rebuttal"] + " _[draft rebuttal from revision assessment -- review before sending]_"
            lines.append(f"**Response:** {resp or '_(not yet drafted)_'}\n")
    return "\n".join(lines)


def write_response_letter(project, out_path: str | Path) -> Path:
    md = export_response_letter(project)
    out = Path(out_path)
    if out.suffix.lower() == ".md":
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        return out
    return markdown_to_docx(md, out)


def vent_to_reply(llm: BaseLLM, project: Project, comment: dict, vent: str) -> dict:
    """Turn the author's raw, private reaction to a comment into the calm,
    evidence-based reply they should actually send."""
    text, ext = project.manuscript_text()
    chunks = chunk_manuscript(text, ext)
    query = comment["comment_text"] + " " + vent
    excerpt = "\n\n".join(c.text for c in find_chunk_for_query(chunks, query, max_chunks=3))[:12000]
    context = refs_block(comment)
    dn = _decoded_notes(comment)
    if dn:
        context += ("\n" if context else "") + "Decoded comment: " + dn
    t = comment.get("triage") or {}
    if t:
        context += ("\n" if context else "") + (f"Revision assessment: reviewer {t.get('agreement')}, planned stance "
                                                f"{t.get('stance')}. {t.get('rationale', '')}")
    result = llm.chat_json([
        {"role": "system", "content": "You help scientists answer peer review calmly and convincingly. "
                                      "You respond only in JSON."},
        {"role": "user", "content": prompts.VENT_TASK.format(
            reviewer=comment["reviewer"], comment=comment["comment_text"], context=context + "\n" if context else "",
            vent=vent.strip(), excerpt=excerpt)},
    ], json_schema=prompts.VENT_SCHEMA, temperature=0.4)
    if not isinstance(result, dict):
        result = {}
    return {"response_text": str(result.get("response_text") or "").strip(),
            "manuscript_change": str(result.get("manuscript_change") or "").strip(),
            "notes_for_author": str(result.get("notes_for_author") or "").strip()}

