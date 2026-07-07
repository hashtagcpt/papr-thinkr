"""Turns raw reviewer/editor letter text into a structured list of discrete
comment items, using the local LLM to do the segmentation. Falls back to a
regex-based splitter if the LLM is unavailable or keeps returning garbage."""
from __future__ import annotations

import re
import sys

from . import prompts
from .llm import OllamaClient, LLMError

# Headers that plausibly start a new reviewer/editor section in a decision letter.
_SECTION_RE = re.compile(
    r"^(Reviewer\s*#?\s*\d+.*|Editorial Board Member.*|Associate Editor.*|"
    r"AE Comments.*|Editor(?:'s)? Comments.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_MAX_CHARS_PER_CALL = 9000


def _clean_section_label(label: str) -> str:
    """Strip common journal-portal boilerplate suffixes off a matched header,
    e.g. 'Reviewer #1 (Comments for the Author (Required)):' -> 'Reviewer #1'."""
    label = re.sub(r"\(Comments? for the Authors?\s*\(?(Required|Optional)?\)?\)?", "", label, flags=re.IGNORECASE)
    label = label.strip(" :()")
    return label or "Reviewer"


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Split raw letter text into (label, body) sections using header lines.
    If no headers are found, returns [("General", text)]."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return [("General", text)]
    sections = []
    for i, m in enumerate(matches):
        label = _clean_section_label(m.group(1).strip()[:80])
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((label, body))
    if matches[0].start() > 0:
        preamble = text[:matches[0].start()].strip()
        if preamble:
            sections.insert(0, ("Preamble / Decision Letter", preamble))
    return sections


def _regex_fallback(label: str, body: str) -> list[dict]:
    """Crude fallback: one comment item per non-trivial paragraph."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    items = []
    for i, para in enumerate(paras, 1):
        if len(para) < 15:
            continue
        items.append({
            "reviewer": label,
            "order": i,
            "location_ref": None,
            "comment_text": para,
            "severity": "unclear",
        })
    return items


def extract_comments(llm: OllamaClient, source_label: str, raw_text: str,
                      use_llm: bool = True) -> list[dict]:
    """Returns a list of comment dicts with keys:
    id, source, reviewer, order, location_ref, comment_text, severity."""
    sections = _split_into_sections(raw_text)
    all_items: list[dict] = []

    for label, body in sections:
        # A section header we matched (e.g. "Reviewer #1 (...)") already tells us
        # definitively who wrote this text -- don't make the model re-guess
        # attribution per-chunk, which is unreliable once a section gets split
        # across multiple LLM calls. Only the unheadered whole-document fallback
        # ("General") needs the model's own judgement for the "reviewer" field.
        known_author = label if label not in ("General",) else None

        # Keep each LLM call to a manageable size; split oversized sections further.
        pieces = [body[i:i + _MAX_CHARS_PER_CALL] for i in range(0, len(body), _MAX_CHARS_PER_CALL)] or [body]
        section_items: list[dict] = []
        for piece in pieces:
            items = None
            if use_llm and llm.is_available():
                try:
                    prompt = prompts.COMMENT_EXTRACTION.format(source_text=piece)
                    if known_author:
                        prompt += f"\n\nAll of the above text is from: {known_author}. Use that exact string as \"reviewer\" for every item."
                    result = llm.chat_json([
                        {"role": "system", "content": "You extract structured data from text and respond only in JSON."},
                        {"role": "user", "content": prompt},
                    ], json_schema=prompts.COMMENT_EXTRACTION_SCHEMA)
                    if isinstance(result, list):
                        items = result
                    elif isinstance(result, dict):
                        items = [result]
                    if items is not None and known_author:
                        for it in items:
                            it["reviewer"] = known_author
                except LLMError as e:
                    print(f"WARNING: LLM comment extraction failed for section '{label}': {e}", file=sys.stderr)
                    items = None
            if items is None:
                print(f"WARNING: falling back to paragraph-splitting for section '{label}' "
                      f"({'LLM unavailable' if not (use_llm and llm.is_available()) else 'LLM returned unusable output'})",
                      file=sys.stderr)
                items = _regex_fallback(label, piece)
            section_items.extend(items)
        all_items.extend(section_items)

    # Assign stable ids and normalize
    normalized = []
    counters: dict[str, int] = {}
    for item in all_items:
        reviewer = str(item.get("reviewer") or "Unknown").strip()
        counters[reviewer] = counters.get(reviewer, 0) + 1
        slug = re.sub(r"[^a-z0-9]+", "-", reviewer.lower()).strip("-") or "unknown"
        comment_id = f"{source_label}:{slug}:{counters[reviewer]}"
        normalized.append({
            "id": comment_id,
            "source": source_label,
            "reviewer": reviewer,
            "order": item.get("order", counters[reviewer]),
            "location_ref": item.get("location_ref"),
            "comment_text": str(item.get("comment_text", "")).strip(),
            "severity": item.get("severity") or "unclear",
            "status": "unaddressed",
            "status_evidence": "",
            "suggested_action": "",
            "author_notes": "",
            "response_text": "",
            "patch_ids": [],
        })

    return [n for n in normalized if n["comment_text"]]
