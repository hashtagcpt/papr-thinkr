"""Propose, validate, preview, and apply find/replace style edits to a
manuscript file. Mirrors the semantics of a precise text-editing tool: a
patch's "find" string must exist verbatim and exactly once in the file
before it is allowed to be applied."""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from . import prompts
from .ingest import chunk_manuscript, find_chunk_for_query
from .llm import OllamaClient, LLMError
from .project import Project


class PatchError(RuntimeError):
    pass


def _occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


def validate_find(file_text: str, find: str) -> tuple[bool, str]:
    if not find:
        return False, "No 'find' text given."
    count = _occurrences(file_text, find)
    if count == 0:
        return False, "Find text was not found verbatim in the current manuscript. It may have changed since this patch was proposed."
    if count > 1:
        return False, f"Find text is ambiguous: it appears {count} times in the manuscript. Needs a more specific span."
    return True, "OK"


def propose_patch(llm: OllamaClient, project: Project, comment: dict,
                   manuscript_label: str | None = None) -> dict:
    path = project.manuscript_path(manuscript_label)
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_manuscript(text, path.suffix.lower())
    relevant = find_chunk_for_query(chunks, comment["comment_text"], max_chunks=2)
    excerpt = "\n\n".join(c.text for c in relevant) if relevant else text[:6000]
    heading = ", ".join(c.heading for c in relevant) if relevant else "(whole document)"

    # cap excerpt size sent to the model
    if len(excerpt) > 12000:
        excerpt = excerpt[:12000]

    prompt = prompts.PATCH_PROPOSAL.format(
        comment_text=comment["comment_text"],
        section_heading=heading,
        excerpt=excerpt,
    )
    try:
        result = llm.chat_json([
            {"role": "system", "content": "You propose precise text edits and respond only in JSON."},
            {"role": "user", "content": prompt},
        ], json_schema=prompts.PATCH_PROPOSAL_SCHEMA)
    except LLMError as e:
        raise PatchError(str(e))

    if not isinstance(result, dict):
        raise PatchError("Model did not return a JSON object for the patch proposal.")

    find = result.get("find")
    replace = result.get("replace")
    rationale = result.get("rationale", "")

    patch = project.add_patch(
        file=str(path), find=find, replace=replace,
        rationale=rationale, comment_id=comment["id"],
    )

    if find:
        ok, msg = validate_find(text, find)
        patch["validated"] = ok
        patch["validation_message"] = msg
    else:
        patch["validated"] = False
        patch["validation_message"] = "Model determined this comment needs more than a text edit."
    project.save()

    comment.setdefault("patch_ids", [])
    comment["patch_ids"].append(patch["id"])
    project.save()

    return patch


def diff_preview(project: Project, patch: dict, context_lines: int = 3) -> str:
    path = Path(patch["file"])
    text = path.read_text(encoding="utf-8", errors="replace")
    find = patch.get("find")
    replace = patch.get("replace")
    if not find:
        return "(no text edit -- see rationale)"

    idx = text.find(find)
    if idx == -1:
        return "(find text no longer present in file -- cannot preview diff)"

    before_text = text[:idx]
    after_text = text[idx + len(find):]

    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()

    ctx_before = before_lines[-context_lines:] if context_lines else []
    ctx_after = after_lines[:context_lines] if context_lines else []

    old_block = "\n".join(ctx_before) + ("\n" if ctx_before else "") + find + ("\n" if ctx_after else "") + "\n".join(ctx_after)
    new_block = "\n".join(ctx_before) + ("\n" if ctx_before else "") + (replace or "") + ("\n" if ctx_after else "") + "\n".join(ctx_after)

    diff = difflib.unified_diff(
        old_block.splitlines(keepends=False),
        new_block.splitlines(keepends=False),
        fromfile="current", tofile="proposed", lineterm="",
    )
    return "\n".join(diff)


def apply_patch(project: Project, patch_id: str) -> dict:
    patch = project.get_patch(patch_id)
    if patch is None:
        raise PatchError(f"No patch with id {patch_id}")
    if patch["status"] == "applied":
        raise PatchError("Patch already applied.")
    find = patch.get("find")
    replace = patch.get("replace")
    if not find:
        raise PatchError("This patch has no text edit to apply (find is null).")

    path = Path(patch["file"])
    text = path.read_text(encoding="utf-8", errors="replace")
    ok, msg = validate_find(text, find)
    if not ok:
        patch["validated"] = False
        patch["validation_message"] = msg
        project.save()
        raise PatchError(msg)

    backup_path = project.backup_file(path)
    new_text = text.replace(find, replace or "", 1)
    path.write_text(new_text, encoding="utf-8")

    patch["status"] = "applied"
    patch["backup_path"] = str(backup_path)
    project.save()
    return patch


def reject_patch(project: Project, patch_id: str) -> dict:
    patch = project.get_patch(patch_id)
    if patch is None:
        raise PatchError(f"No patch with id {patch_id}")
    patch["status"] = "rejected"
    project.save()
    return patch


def revise_patch(llm: OllamaClient, project: Project, patch_id: str, feedback: str) -> dict:
    """Ask the model to refine an existing proposed patch given user feedback."""
    patch = project.get_patch(patch_id)
    if patch is None:
        raise PatchError(f"No patch with id {patch_id}")
    comment = project.get_comment(patch["comment_id"]) if patch["comment_id"] else None

    prompt = (
        f"You previously proposed this edit:\n\n"
        f"find: {json.dumps(patch.get('find'))}\n"
        f"replace: {json.dumps(patch.get('replace'))}\n"
        f"rationale: {patch.get('rationale')}\n\n"
        f"The author gave this feedback: \"{feedback}\"\n\n"
        f"Revise the proposal accordingly. Respond with ONLY a JSON object with keys "
        f"\"find\" (string or null), \"replace\" (string or null), \"rationale\" (string)."
    )
    try:
        result = llm.chat_json([
            {"role": "system", "content": "You propose precise text edits and respond only in JSON."},
            {"role": "user", "content": prompt},
        ], json_schema=prompts.PATCH_PROPOSAL_SCHEMA)
    except LLMError as e:
        raise PatchError(str(e))

    if not isinstance(result, dict):
        raise PatchError("Model did not return a JSON object for the revised patch.")

    patch["find"] = result.get("find")
    patch["replace"] = result.get("replace")
    patch["rationale"] = result.get("rationale", patch.get("rationale", ""))
    patch["status"] = "proposed"

    path = Path(patch["file"])
    text = path.read_text(encoding="utf-8", errors="replace")
    if patch["find"]:
        ok, msg = validate_find(text, patch["find"])
        patch["validated"] = ok
        patch["validation_message"] = msg
    else:
        patch["validated"] = False
        patch["validation_message"] = "Model determined this comment needs more than a text edit."
    project.save()
    return patch
