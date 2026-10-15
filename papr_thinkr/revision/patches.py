"""Propose, validate, preview, apply and revert find/replace edits to a
manuscript file. Mirrors the semantics of a precise text-editing tool: a
patch's "find" string must exist verbatim and exactly once before it is
allowed to be applied.

Only plain-text manuscripts (.tex/.md/.txt/...) can be edited in place. For
a .docx/.pdf manuscript, proposals are still generated (and shown in the
report) but must be applied by hand. Files are written back in their
original encoding and line-ending style, after a backup."""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from .. import prompts
from ..inputs.chunking import chunk_manuscript, find_chunk_for_query
from ..inputs.line_refs import refs_block
from ..inputs.documents import (EDITABLE_EXTS, flatten_tex, is_editable, manuscript_review_text, read_text_file,
                                tex_component_files, write_text_file)
from ..llm import BaseLLM, LLMError
from ..project import Project
from .provenance import capture_snapshot, file_hash


class PatchError(RuntimeError):
    pass


def _norm_nl(s: str | None) -> str | None:
    return s.replace("\r\n", "\n") if isinstance(s, str) else s


def validate_find(file_text: str, find: str | None) -> tuple[bool, str]:
    if not find:
        return False, "No 'find' text given."
    count = file_text.count(find)
    if count == 0:
        return False, "Find text was not found verbatim in the current manuscript. It may have changed since this patch was proposed."
    if count > 1:
        return False, f"Find text is ambiguous: it appears {count} times in the manuscript. Needs a more specific span."
    return True, "OK"


def candidate_files(main: Path) -> list[Path]:
    """Files a patch may target: the main manuscript plus, for LaTeX, every
    file it \\input's."""
    if main.suffix.lower() == ".tex":
        files = tex_component_files(main)
        return files or [main]
    return [main]


def locate(main: Path, find: str) -> tuple[Path | None, str]:
    """Find the single file containing `find` exactly once across the
    manuscript's component files."""
    hits = []
    total = 0
    for f in candidate_files(main):
        try:
            text = read_text_file(f)[0]
        except Exception:
            continue
        n = text.count(find)
        total += n
        if n:
            hits.append(f)
    if total == 0:
        return None, ("Find text was not found verbatim in the current manuscript. It may have changed "
                      "since this patch was proposed.")
    if total > 1:
        return None, f"Find text is ambiguous: it appears {total} times in the manuscript. Needs a more specific span."
    return hits[0], "OK"


def _validate_patch(project: Project, patch: dict) -> None:
    find = _norm_nl(patch.get("find"))
    main = Path(patch.get("manuscript") or patch["file"])
    if not find:
        patch["validated"] = False
        patch["validation_message"] = "No text edit proposed -- this comment needs more than a wording change."
        return
    if patch.get("replace") is None:
        patch["validated"] = False
        patch["validation_message"] = "The proposal has no replacement text."
        return
    if not is_editable(main):
        patch["validated"] = False
        patch["validation_message"] = (f"{main.suffix} manuscripts can't be edited in place -- apply this change "
                                       f"by hand in your word processor.")
        return
    target, msg = locate(main, find)
    patch["validated"] = target is not None
    patch["validation_message"] = msg
    if target is not None:
        patch["file"] = str(target)


def _excerpt_for(project: Project, query: str, manuscript_label: str | None = None) -> tuple[Path, str, str]:
    path = project.manuscript_path(manuscript_label)
    if is_editable(path) and path.suffix.lower() != ".tex":
        text = read_text_file(path)[0]
    else:
        # flattened LaTeX keeps the raw markup the model must copy verbatim
        text = flatten_tex(path) if path.suffix.lower() == ".tex" else manuscript_review_text(path)
    chunks = chunk_manuscript(text, path.suffix.lower())
    relevant = find_chunk_for_query(chunks, query, max_chunks=2)
    excerpt = "\n\n".join(c.text for c in relevant) if relevant else text[:8000]
    heading = ", ".join(c.heading for c in relevant) if relevant else "(whole document)"
    return path, heading, excerpt[:14000]


def propose_patch(llm: BaseLLM, project: Project, comment: dict,
                  manuscript_label: str | None = None) -> dict:
    path, heading, excerpt = _excerpt_for(project, comment["comment_text"], manuscript_label)
    extra = ""
    refs = refs_block(comment)
    if refs:
        extra += "\n" + refs + "\n"
    d = comment.get("decoded") or {}
    if d.get("plain_language"):
        extra += f"\nWhat the reviewer actually wants: {d['plain_language']}\n"
    if d.get("misunderstanding", "none") != "none" and d.get("clarifying_edit"):
        extra += (f"\nThe reviewer misread the paper ({d['misunderstanding']}): {d.get('misunderstanding_explanation', '')} "
                  f"Prefer a small clarifying edit, e.g.: {d['clarifying_edit']}\n")
    t = comment.get("triage") or {}
    if t.get("required_changes"):
        extra += "\nPlanned changes for this comment (from the revision assessment): " + "; ".join(t["required_changes"]) + "\n"
    if t.get("stance") in ("rebut",):
        extra += "\nNote: the revision assessment recommends rebutting this comment; propose at most a small clarifying edit.\n"
    cr = comment.get("critique") or {}
    if cr.get("suggestion"):
        extra += f"\nSuggested fix: {cr['suggestion']}\n"
    if cr.get("quote"):
        extra += f"\nThe problem text quoted by the reviewer: \"{cr['quote']}\"\n"
    related = {comment["id"]}
    for group in comment.get("joint_guidance") or []:
        if group.get("compatibility") in ("jointly_satisfiable", "compromise"):
            related.update(group["comment_ids"])
        extra += (f"\nConsider these reviewers together ({', '.join(group['comment_ids'])}): {group['approach']}\n"
                  f"Compatible planned changes: {'; '.join(group['manuscript_changes'])}\n"
                  f"Tradeoff: {group.get('tradeoff') or 'none recorded'}. Prefer one coherent edit when it can satisfy multiple requests.\n")
    if not is_editable(path):
        extra += ("\nThe excerpt was converted from a word-processor file, so markup may differ slightly from the "
                  "original; still copy 'find' exactly from the excerpt.\n")

    prompt = prompts.PATCH_PROPOSAL.format(comment_text=comment["comment_text"], section_heading=heading,
                                           excerpt=excerpt, extra_context=extra)
    try:
        result = llm.chat_json([
            {"role": "system", "content": "You propose precise text edits and respond only in JSON."},
            {"role": "user", "content": prompt},
        ], json_schema=prompts.PATCH_PROPOSAL_SCHEMA)
    except LLMError as e:
        raise PatchError(str(e))
    if not isinstance(result, dict):
        raise PatchError("Model did not return a JSON object for the patch proposal.")

    patch = {"file": str(path), "manuscript": str(path), "find": _norm_nl(result.get("find")),
             "replace": _norm_nl(result.get("replace")), "rationale": result.get("rationale", "")}
    _validate_patch(project, patch)
    saved = project.add_patch(file=patch["file"], find=patch["find"], replace=patch["replace"],
                              rationale=patch["rationale"], comment_id=comment["id"],
                              manuscript=patch["manuscript"], validated=patch["validated"],
                              validation_message=patch["validation_message"], related_comment_ids=sorted(related))
    return saved


def diff_preview(project: Project, patch: dict, context_lines: int = 3) -> str:
    find = _norm_nl(patch.get("find"))
    replace = _norm_nl(patch.get("replace")) or ""
    if not find:
        return "(no text edit -- see rationale)"
    path = Path(patch["file"])
    if not path.exists():
        return f"(file not found: {path})"
    if patch.get("status") == "applied":
        return "(already applied)"
    if is_editable(path):
        text = read_text_file(path)[0]
    else:
        text = manuscript_review_text(path)
    idx = text.find(find)
    if idx == -1:
        return "(find text no longer present in file -- cannot preview diff)"
    new_text = text[:idx] + replace + text[idx + len(find):]
    diff = difflib.unified_diff(text.splitlines(), new_text.splitlines(), fromfile="current",
                                tofile="proposed", lineterm="", n=context_lines)
    return "\n".join(diff) or "(no change)"


def apply_patch(project: Project, patch_id: str) -> dict:
    before = capture_snapshot(project, "Before manuscript edit")
    with project.mutate():
        patch = project.get_patch(patch_id)
        if patch is None:
            raise PatchError(f"No patch with id {patch_id}")
        if patch["status"] == "applied":
            raise PatchError("Patch already applied.")
        if patch["status"] == "rejected":
            raise PatchError("Patch was rejected; ask for a revision instead.")
        _validate_patch(project, patch)
        if not patch["validated"]:
            raise PatchError(patch["validation_message"])
        path = Path(patch["file"])
        text, meta = read_text_file(path)
        find, replace = _norm_nl(patch["find"]), _norm_nl(patch["replace"]) or ""
        backup_path = project.backup_file(path)
        try:
            write_text_file(path, text.replace(find, replace, 1), meta)
        except Exception as e:
            raise PatchError(str(e))
        patch["status"] = "applied"
        patch["backup_path"] = str(backup_path)
        patch["file_encoding"] = meta.encoding
        patch["before_snapshot_id"] = before["id"]
        patch["applied_sha256"] = file_hash(path)
        return dict(patch)


def revert_patch(project: Project, patch_id: str) -> dict:
    """Undo an applied patch by swapping the replacement text back (only if it
    is still present exactly once), after taking another backup."""
    capture_snapshot(project, "Before reverting manuscript edit")
    with project.mutate():
        patch = project.get_patch(patch_id)
        if patch is None:
            raise PatchError(f"No patch with id {patch_id}")
        if patch["status"] != "applied":
            raise PatchError("Only applied patches can be reverted.")
        path = Path(patch["file"])
        text, meta = read_text_file(path)
        replace, find = _norm_nl(patch.get("replace")) or "", _norm_nl(patch["find"])
        if replace:
            n = text.count(replace)
            if n != 1:
                raise PatchError(f"Can't revert automatically: the inserted text now appears {n} times. "
                                 f"Restore from the backup instead: {patch.get('backup_path')}")
            new_text = text.replace(replace, find, 1)
        else:
            raise PatchError(f"This patch deleted text, so it can't be located for an automatic revert. "
                             f"Restore from the backup instead: {patch.get('backup_path')}")
        project.backup_file(path)
        write_text_file(path, new_text, meta)
        patch["status"] = "reverted"
        return dict(patch)


def reject_patch(project: Project, patch_id: str) -> dict:
    with project.mutate():
        patch = project.get_patch(patch_id)
        if patch is None:
            raise PatchError(f"No patch with id {patch_id}")
        if patch["status"] == "applied":
            raise PatchError("This patch is already applied to the file; use Revert instead.")
        patch["status"] = "rejected"
        return dict(patch)


def revise_patch(llm: BaseLLM, project: Project, patch_id: str, feedback: str) -> dict:
    """Ask the model to refine an existing proposed patch given user feedback."""
    patch = project.get_patch(patch_id)
    if patch is None:
        raise PatchError(f"No patch with id {patch_id}")
    if patch["status"] == "applied":
        raise PatchError("This patch is already applied; revert it first, or propose a new edit.")
    comment = project.get_comment(patch["comment_id"]) if patch.get("comment_id") else None
    comment_text = comment["comment_text"] if comment else "(comment no longer exists)"
    _path, _heading, excerpt = _excerpt_for(project, (patch.get("find") or "") + " " + comment_text)
    prompt = prompts.PATCH_REVISION.format(
        find=json.dumps(patch.get("find")), replace=json.dumps(patch.get("replace")),
        rationale=patch.get("rationale"), feedback=feedback, comment_text=comment_text, excerpt=excerpt)
    try:
        result = llm.chat_json([
            {"role": "system", "content": "You propose precise text edits and respond only in JSON."},
            {"role": "user", "content": prompt},
        ], json_schema=prompts.PATCH_PROPOSAL_SCHEMA)
    except LLMError as e:
        raise PatchError(str(e))
    if not isinstance(result, dict):
        raise PatchError("Model did not return a JSON object for the revised patch.")

    with project.mutate():
        p = project.get_patch(patch_id)
        p["find"] = _norm_nl(result.get("find"))
        p["replace"] = _norm_nl(result.get("replace"))
        p["rationale"] = result.get("rationale", p.get("rationale", ""))
        p["status"] = "proposed"
        p.setdefault("manuscript", p["file"])
        p.setdefault("history", []).append({"feedback": feedback})
        _validate_patch(project, p)
        return dict(p)


__all__ = ["PatchError", "validate_find", "propose_patch", "diff_preview", "apply_patch",
           "revert_patch", "reject_patch", "revise_patch", "EDITABLE_EXTS"]
