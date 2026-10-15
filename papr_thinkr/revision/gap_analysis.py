"""Gap analysis: re-reads the current manuscript and judges, for every
comment, whether it is now addressed and what is still missing."""
from __future__ import annotations

import time

from .. import prompts
from ..inputs.chunking import chunk_manuscript, fit_text
from ..llm import BaseLLM, LLMCancelled, LLMError
from ..project import Project
from .cache import ReviewCache
from .provenance import capture_snapshot, digest


# Gap-analysis statuses -> comment statuses used by the UI
_STATUS_MAP = {"addressed": "addressed", "partially_addressed": "partially_addressed",
               "not_addressed": "unaddressed", "unclear": "unclear"}


def run_gap_analysis(llm: BaseLLM, project: Project, manuscript_label: str | None = None,
                     progress=None, comment_ids: list[str] | None = None, force: bool = False) -> list[dict]:
    progress = progress or (lambda msg, frac=None: None)
    snapshot = capture_snapshot(project, "Gap analysis")
    cache = ReviewCache(project, llm, snapshot, force=force)
    text, ext = project.manuscript_text(manuscript_label)
    chunks = chunk_manuscript(text, ext)
    comments = [c for c in project.state["comments"] if comment_ids is None or c["id"] in comment_ids]
    if not comments:
        return project.state["comments"]

    plan_text = project.config.get("gap_plan_text")
    existing_plan_block = ""
    if plan_text:
        existing_plan_block = (
            "\nEXISTING AUTHOR GAP/REVISION NOTES (may be partially stale -- use as "
            "context, not ground truth; the manuscript text above is authoritative):\n"
            f"---\n{plan_text[:6000]}\n---\n")

    batch_size = 6 if llm.is_local else 12
    budget = int(llm.context_chars * 0.8) - len(existing_plan_block) - 8000
    whole, note = fit_text(text, chunks, budget)
    whole_fits = note is None or note.startswith("References")
    results: dict[str, dict] = {}
    errors: list[str] = []
    n_batches = (len(comments) + batch_size - 1) // batch_size
    for bi, i in enumerate(range(0, len(comments), batch_size)):
        batch = comments[i:i + batch_size]
        progress(f"Gap analysis batch {bi + 1}/{n_batches}...", bi / n_batches)
        comments_block = "\n\n".join(
            f"[{c['id']}] ({c['reviewer']}, {c.get('severity', 'unclear')}): {c['comment_text']}" for c in batch)
        if whole_fits:
            manuscript_text = whole       # identical prefix every batch -> prompt cache hits
        else:
            manuscript_text, _ = fit_text(text, chunks, budget,
                                          focus_query=" ".join(c["comment_text"] for c in batch))
        prompt = prompts.GAP_ANALYSIS.format(manuscript_text=manuscript_text, comments_block=comments_block,
                                             existing_plan_block=existing_plan_block)
        try:
            result = cache.chat_json([
                {"role": "system", "content": "You assess documents against a checklist and respond only in JSON."},
                {"role": "user", "content": prompt},
            ], json_schema=prompts.GAP_ANALYSIS_SCHEMA,
                cache_if=lambda rows: isinstance(rows, list) and
                {str(r.get("comment_id", "")).strip("[] ") for r in rows if isinstance(r, dict)} == {c["id"] for c in batch})
        except LLMCancelled:
            raise
        except LLMError as e:
            errors.append(str(e))
            continue
        if isinstance(result, dict):
            result = [result]
        rows = [r for r in (result or []) if isinstance(r, dict)]
        ids = [c["id"] for c in batch]
        matched = {str(r.get("comment_id", "")).strip("[] "): r for r in rows
                   if str(r.get("comment_id", "")).strip("[] ") in ids}
        if len(matched) != len(batch):
            errors.append("Some assessment IDs did not match the requested comments; those items were left unchanged.")
        results.update(matched)
        # Persist every successful batch, even if a later call is cancelled.
        expected = {c["id"]: c["comment_text"] for c in batch}
        with project.mutate():
            for c in project.state["comments"]:
                r = matched.get(c["id"])
                if not r or c["comment_text"] != expected.get(c["id"]):
                    continue
                assessed = _STATUS_MAP.get(r.get("status"), "unclear")
                c["assessed_status"] = assessed
                if c.get("status") != "wontfix":
                    c["status"] = assessed
                c["status_evidence"] = r.get("evidence", "") or ""
                c["suggested_action"] = r.get("suggested_action", "") or ""
                c["gap_snapshot_id"] = snapshot["id"]
                c["gap_request_hash"] = digest(c["comment_text"])

    with project.mutate():
        project.state["gap_analysis_generated_at"] = time.time()
        if comment_ids is None and len(results) == len(comments) and not errors:
            project.state["gap_analysis_snapshot_id"] = snapshot["id"]
        project.state["gap_analysis_cache"] = cache.summary()
        if errors:
            project.state["gap_analysis_errors"] = errors[:5]
        else:
            project.state.pop("gap_analysis_errors", None)
    if errors and not results:
        raise LLMError(f"Gap analysis failed: {errors[0]}")
    return project.state["comments"]
