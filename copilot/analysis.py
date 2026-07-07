"""Higher-level operations that combine the manuscript + comments + LLM:
gap analysis (status of each comment against current manuscript text),
project chat (co-author thinking partner), and response-letter drafting."""
from __future__ import annotations

import time

from . import prompts
from .ingest import find_chunk_for_query, chunk_manuscript
from .llm import OllamaClient, LLMError
from .project import Project


def _manuscript_text(project: Project, manuscript_label: str | None = None) -> str:
    path = project.manuscript_path(manuscript_label)
    return path.read_text(encoding="utf-8", errors="replace")


def run_gap_analysis(llm: OllamaClient, project: Project,
                      manuscript_label: str | None = None) -> list[dict]:
    text = _manuscript_text(project, manuscript_label)
    comments = project.state["comments"]
    if not comments:
        return []

    # batch comments to keep prompt size reasonable
    batch_size = 12
    results: list[dict] = []
    for i in range(0, len(comments), batch_size):
        batch = comments[i:i + batch_size]
        comments_block = "\n\n".join(
            f"[{c['id']}] ({c['reviewer']}, {c.get('severity', 'unclear')}): {c['comment_text']}"
            for c in batch
        )
        manuscript_excerpt = text if len(text) < 20000 else text[:20000]
        plan_text = project.config.get("gap_plan_text")
        existing_plan_block = ""
        if plan_text:
            existing_plan_block = (
                "\nEXISTING AUTHOR GAP/REVISION NOTES (may be partially stale -- use as "
                "context, not ground truth; the manuscript text above is authoritative):\n"
                f"---\n{plan_text[:6000]}\n---\n"
            )
        prompt = prompts.GAP_ANALYSIS.format(
            manuscript_text=manuscript_excerpt,
            comments_block=comments_block,
            existing_plan_block=existing_plan_block,
        )
        try:
            result = llm.chat_json([
                {"role": "system", "content": "You assess documents against a checklist and respond only in JSON."},
                {"role": "user", "content": prompt},
            ], json_schema=prompts.GAP_ANALYSIS_SCHEMA)
        except LLMError as e:
            result = [{"comment_id": c["id"], "status": "unclear",
                       "evidence": "", "suggested_action": f"Gap analysis failed: {e}"} for c in batch]
        if isinstance(result, dict):
            result = [result]
        if isinstance(result, list):
            results.extend(result)

    by_id = {r.get("comment_id"): r for r in results if isinstance(r, dict)}
    for c in comments:
        r = by_id.get(c["id"])
        if r:
            c["status"] = r.get("status", c.get("status", "unclear"))
            c["status_evidence"] = r.get("evidence", "")
            c["suggested_action"] = r.get("suggested_action", "")

    project.state["gap_analysis_generated_at"] = time.time()
    project.save()
    return comments


def project_chat_reply(llm: OllamaClient, project: Project, thread_id: str,
                        user_message: str, manuscript_label: str | None = None) -> str:
    project.append_message(thread_id, "user", user_message)
    thread = project.get_thread(thread_id)

    text = _manuscript_text(project, manuscript_label)
    chunks = chunk_manuscript(text, project.manuscript_path(manuscript_label).suffix.lower())
    relevant = find_chunk_for_query(chunks, user_message, max_chunks=3)
    excerpt = "\n\n".join(f"### {c.heading}\n{c.text}" for c in relevant)
    if len(excerpt) > 14000:
        excerpt = excerpt[:14000]

    open_comments = [c for c in project.state["comments"] if c.get("status") != "addressed"]
    comments_summary = "\n".join(
        f"- [{c['id']}] ({c['reviewer']}, {c.get('status', 'unaddressed')}): {c['comment_text'][:200]}"
        for c in open_comments[:25]
    ) or "(no outstanding comments loaded)"

    if thread.get("comment_id"):
        focused = project.get_comment(thread["comment_id"])
        focus_block = f"\nYou are currently focused on this specific comment:\n[{focused['id']}] {focused['comment_text']}\n" if focused else ""
    else:
        focus_block = ""

    system = prompts.COAUTHOR_SYSTEM.format(
        journal=project.config.get("journal", "the target journal"),
        title=project.config.get("title") or project.config.get("name", ""),
    )
    plan_text = project.config.get("gap_plan_text")
    plan_block = f"\n\nEXISTING AUTHOR GAP/REVISION NOTES (context only, may be stale):\n{plan_text[:4000]}" if plan_text else ""

    system += f"\n\nRELEVANT MANUSCRIPT EXCERPT(S):\n{excerpt}\n\nOUTSTANDING REVIEWER COMMENTS:\n{comments_summary}\n{focus_block}{plan_block}"

    messages = [{"role": "system", "content": system}]
    for m in thread["messages"][-20:]:
        if m["role"] in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})

    reply = llm.chat(messages, temperature=0.5)
    project.append_message(thread_id, "assistant", reply)
    return reply


def draft_response_letter_item(llm: OllamaClient, project: Project, comment: dict) -> str:
    patches = [project.get_patch(pid) for pid in comment.get("patch_ids", [])]
    applied = [p for p in patches if p and p.get("status") == "applied"]
    if applied:
        change_summary = "\n".join(
            f"- Rationale: {p['rationale']}\n  Replaced: \"{p.get('find', '')}\"\n  With: \"{p.get('replace', '')}\""
            for p in applied
        )
    else:
        change_summary = "(no manuscript edit applied yet)"

    prompt = prompts.RESPONSE_LETTER_ITEM.format(
        reviewer=comment["reviewer"],
        comment_text=comment["comment_text"],
        status=comment.get("status", "unaddressed"),
        change_summary=change_summary,
        author_notes=comment.get("author_notes") or "(none)",
    )
    try:
        result = llm.chat_json([
            {"role": "system", "content": "You draft formal peer-review response letters and respond only in JSON."},
            {"role": "user", "content": prompt},
        ], json_schema=prompts.RESPONSE_LETTER_ITEM_SCHEMA)
    except LLMError as e:
        return f"[Could not draft response: {e}]"

    if isinstance(result, dict):
        text = result.get("response_text", "")
        comment["response_text"] = text
        project.save()
        return text
    return ""


def export_response_letter(project: Project) -> str:
    lines = [f"# Point-by-Point Response to Reviewers", f"## {project.config.get('title') or project.config.get('name')}", ""]
    by_reviewer: dict[str, list[dict]] = {}
    for c in project.state["comments"]:
        by_reviewer.setdefault(c["reviewer"], []).append(c)

    for reviewer, items in by_reviewer.items():
        lines.append(f"\n## {reviewer}\n")
        for i, c in enumerate(sorted(items, key=lambda x: x.get("order", 0)), 1):
            lines.append(f"**Comment {i}:** {c['comment_text']}\n")
            lines.append(f"**Response:** {c.get('response_text') or '_(not yet drafted)_'}\n")
    return "\n".join(lines)
