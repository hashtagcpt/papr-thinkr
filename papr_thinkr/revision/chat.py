"""The co-author chat: a thinking partner grounded in the manuscript and the
comments, optionally focused on one comment."""
from __future__ import annotations

from .. import prompts
from ..inputs.chunking import chunk_manuscript, find_chunk_for_query
from ..inputs.line_refs import refs_block
from ..llm import BaseLLM
from ..project import Project


def _venue_clause(project: Project) -> str:
    j = (project.config.get("journal") or "").strip()
    return f"undergoing peer review at {j}" if j else "undergoing peer review"


def project_chat_reply(llm: BaseLLM, project: Project, thread_id: str,
                       user_message: str, manuscript_label: str | None = None) -> str:
    thread = project.get_thread(thread_id, create=False)
    history = [m for m in thread.get("messages", []) if m["role"] in ("user", "assistant")]

    text, ext = project.manuscript_text(manuscript_label)
    chunks = chunk_manuscript(text, ext)
    focused = project.get_comment(thread["comment_id"]) if thread.get("comment_id") else None
    query = user_message + (" " + focused["comment_text"] if focused else "")
    budget = max(6000, int(llm.context_chars * 0.5))
    if len(text) <= budget:
        excerpt = text
    else:
        relevant = find_chunk_for_query(chunks, query, max_chunks=4)
        excerpt = "\n\n".join(f"### {c.heading}\n{c.text}" for c in relevant)[:budget]

    open_comments = [c for c in project.state["comments"] if c.get("status") not in ("addressed", "wontfix")]
    comments_summary = "\n".join(
        f"- [{c['id']}] ({c['reviewer']}, {c.get('status', 'unaddressed')}): {' '.join(c['comment_text'].split())[:200]}"
        for c in open_comments[:40]) or "(no outstanding comments)"

    focus_block = ""
    if focused:
        focus_block = f"\nYou are currently focused on this specific comment:\n[{focused['id']}] {focused['comment_text']}\n"
        t = focused.get("triage")
        if t:
            focus_block += (f"Revision assessment so far: reviewer is {t['agreement']}; planned stance {t['stance']}; "
                            f"effort {t['effort']['hours_low']}-{t['effort']['hours_high']} h. {t.get('rationale', '')}\n")
        cr = focused.get("critique")
        if cr:
            focus_block += f"Suggested fix: {cr.get('suggestion', '')}\n"
        refs = refs_block(focused)
        if refs:
            focus_block += refs + "\n"
        d = focused.get("decoded") or {}
        if d.get("plain_language"):
            focus_block += f"Decoded: {d['plain_language']} Underlying concern: {d.get('underlying_concern', '')}\n"
            if d.get("misunderstanding", "none") != "none":
                focus_block += (f"Possible misreading ({d['misunderstanding']}): {d.get('misunderstanding_explanation', '')} "
                                f"Manuscript: \"{d.get('manuscript_evidence', '')}\"\n")

    system = prompts.COAUTHOR_SYSTEM.format(
        venue_clause=_venue_clause(project),
        title=project.config.get("title") or project.config.get("name", ""))
    plan_text = project.config.get("gap_plan_text")
    plan_block = f"\n\nEXISTING AUTHOR GAP/REVISION NOTES (context only, may be stale):\n{plan_text[:4000]}" if plan_text else ""
    label = "MANUSCRIPT" if excerpt is text else "RELEVANT MANUSCRIPT EXCERPT(S)"
    system += f"\n\n{label}:\n{excerpt}\n\nOUTSTANDING COMMENTS:\n{comments_summary}\n{focus_block}{plan_block}"

    messages = [{"role": "system", "content": system}]
    for m in history[-20:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_message})

    # Only persist the exchange once the model has answered, so a failed call
    # doesn't leave an orphaned question that gets duplicated on retry.
    reply = llm.chat(messages, temperature=0.5)
    project.append_message(thread_id, "user", user_message)
    project.append_message(thread_id, "assistant", reply)
    return reply
