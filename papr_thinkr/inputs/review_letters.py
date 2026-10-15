"""Turns raw reviewer/editor letter text into a structured list of discrete
comment items, using the LLM to do the segmentation. Falls back to a
regex-based splitter if the LLM is unavailable or keeps returning garbage."""
from __future__ import annotations

import difflib
import re
import sys

from .. import prompts
from ..llm import BaseLLM, LLMCancelled, LLMError
from .chunking import split_text

# Headers that plausibly start a new reviewer/editor section in a decision letter.
_SECTION_RE = re.compile(
    r"^[ \t>*#_-]*("
    r"(?:Comments?\s+(?:from|of|by)\s+)?(?:Reviewer|Referee)\s*(?:#|No\.?|number)?\s*:?\s*[0-9IVX]+\b.*"
    r"|(?:Reviewer|Referee)\s+[A-C]\b.*"
    r"|Editorial Board Member.*|(?:Associate|Academic|Handling|Guest|Senior|Section|Deputy)\s+Editor.*"
    r"|AE\s+Comments.*|Editor(?:'s|s')?\s+Comments.*|Comments\s+(?:from|of|by)\s+the\s+Editor.*"
    r"|Editor\s*:?\s*$"
    r")$",
    re.IGNORECASE | re.MULTILINE,
)

_MAX_CHARS_PER_CALL = 9000
USER_FIELDS = ("status", "status_evidence", "suggested_action", "author_notes", "response_text",
               "patch_ids", "triage", "assessed_status", "revision_actions", "response_check",
               "response_snapshot_id", "gap_snapshot_id", "assessment_snapshot_id")


def _clean_section_label(label: str) -> str:
    """Strip common journal-portal boilerplate suffixes off a matched header,
    e.g. 'Reviewer #1 (Comments for the Author (Required)):' -> 'Reviewer #1'."""
    label = re.sub(r"[*_#>]+", "", label)
    label = re.sub(r"\(Comments? (?:for|to) the Authors?\s*\(?(Required|Optional)?\)?\)?", "", label, flags=re.I)
    label = re.sub(r"^Comments?\s+(?:from|of|by)\s+(the\s+)?", "", label, flags=re.I)
    label = label.strip(" :()-")
    m = re.match(r"^(Reviewer|Referee)\s*(?:#|No\.?|number)?\s*:?\s*([0-9IVX]+|[A-C])\b", label, re.I)
    if m:
        return f"Reviewer {m.group(2).upper()}"
    return label[:60] or "Reviewer"


def _looks_like_header(line: str) -> bool:
    """'Reviewer #2 (Comments for the Author):' is a header; 'Reviewer 2 felt
    the statistics were weak.' is a sentence in the editor's letter."""
    s = re.sub(r"\([^)]*\)", "", line).strip(" \t*#_>:-")
    words = s.split()
    if len(line.strip()) > 120 or len(words) > 10:
        return False
    if s.endswith(".") and len(words) > 4:
        return False
    return True


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Split raw letter text into (label, body) sections using header lines.
    If no headers are found, returns [("General", text)]."""
    matches = [m for m in _SECTION_RE.finditer(text) if _looks_like_header(m.group(0))]
    if not matches:
        return [("General", text)]
    sections = []
    if matches[0].start() > 0:
        preamble = text[:matches[0].start()].strip()
        if preamble:
            sections.append(("Editor", preamble))
    for i, m in enumerate(matches):
        label = _clean_section_label(m.group(1).strip()[:120])
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((label, body))
    return sections


def _regex_fallback(label: str, body: str) -> list[dict]:
    """Crude fallback: one comment item per numbered point or non-trivial paragraph."""
    numbered = re.split(r"\n\s*(?=(?:\(?\d{1,2}[.)]|[-*•])\s+)", "\n" + body)
    paras = [p.strip() for p in numbered if p.strip()]
    if len(paras) <= 1:
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    items = []
    for para in paras:
        if len(para) < 25:
            continue
        loc = re.search(r"\b(lines?\s+\d+(?:\s*[-–]\s*\d+)?|fig(?:ure)?\.?\s*S?\d+[a-z]?|table\s*S?\d+)", para, re.I)
        items.append({
            "reviewer": label,
            "location_ref": loc.group(0) if loc else None,
            "comment_text": para,
            "severity": "unclear",
        })
    return items


def extract_comments(llm: BaseLLM, source_label: str, raw_text: str,
                     use_llm: bool = True, progress=None) -> list[dict]:
    """Returns a list of comment dicts with keys:
    id, source, reviewer, order, location_ref, comment_text, severity, ..."""
    sections = _split_into_sections(raw_text)
    all_items: list[dict] = []
    llm_ok = use_llm and llm is not None and llm.is_available()

    for label, body in sections:
        # A section header we matched (e.g. "Reviewer #1 (...)") already tells us
        # who wrote this text -- don't make the model re-guess attribution
        # per-chunk. Only the unheadered whole-document fallback needs the
        # model's own judgement for the "reviewer" field.
        known_author = label if label != "General" else None
        for piece in split_text(body, _MAX_CHARS_PER_CALL):
            items = None
            if llm_ok:
                try:
                    prompt = prompts.COMMENT_EXTRACTION.format(source_text=piece)
                    if known_author:
                        prompt += (f"\n\nAll of the above text is from: {known_author}. "
                                   f"Use that exact string as \"reviewer\" for every item.")
                    result = llm.chat_json([
                        {"role": "system", "content": "You extract structured data from text and respond only in JSON."},
                        {"role": "user", "content": prompt},
                    ], json_schema=prompts.COMMENT_EXTRACTION_SCHEMA, think=False)
                    if isinstance(result, list):
                        items = [it for it in result if isinstance(it, dict)]
                    elif isinstance(result, dict):
                        items = [result]
                    if items is not None and known_author:
                        for it in items:
                            it["reviewer"] = known_author
                except LLMCancelled:
                    raise
                except LLMError as e:
                    print(f"WARNING: LLM comment extraction failed for section '{label}': {e}", file=sys.stderr)
                    if progress:
                        progress(f"LLM extraction failed for '{label}', using paragraph splitting: {e}")
                    items = None
            if items is None:
                items = _regex_fallback(known_author or "Reviewer", piece)
            all_items.extend(items)

    # Assign stable, URL-safe ids and sequential order (the model's own
    # "order" restarts in every chunk, so it can't be trusted for sorting).
    source_slug = re.sub(r"[^a-z0-9]+", "-", source_label.lower()).strip("-")[:30] or "source"
    normalized = []
    counters: dict[str, int] = {}
    for item in all_items:
        text = str(item.get("comment_text") or "").strip()
        if not text:
            continue
        loc = str(item.get("location_ref") or "").strip()
        if loc and loc.lower() not in text.lower() and re.match(r"(?i)(lines?|ll?\.|fig|figure|table|page|p\.)\s*\S", loc):
            text = f"{loc}: {text}"      # the model stripped "Line 24:" off the front -- put it back
        reviewer = str(item.get("reviewer") or "Unknown").strip()[:60]
        counters[reviewer] = counters.get(reviewer, 0) + 1
        slug = re.sub(r"[^a-z0-9]+", "-", reviewer.lower()).strip("-") or "unknown"
        severity = item.get("severity") if item.get("severity") in ("major", "minor", "editorial") else "unclear"
        normalized.append({
            "id": f"{source_slug}.{slug}.{counters[reviewer]}",
            "origin": "reviewer",
            "source": source_label,
            "reviewer": reviewer,
            "order": counters[reviewer],
            "location_ref": item.get("location_ref"),
            "comment_text": text,
            "severity": severity,
            "status": "unaddressed",
            "status_evidence": "",
            "suggested_action": "",
            "author_notes": "",
            "response_text": "",
            "patch_ids": [],
        })
    return normalized


def _similarity(a: str, b: str) -> float:
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) / max(len(a), len(b)) < 0.5:
        return 0.0
    return difflib.SequenceMatcher(None, a[:2000], b[:2000], autojunk=False).ratio()


def merge_preserving_user_work(old_items: list[dict], new_items: list[dict],
                               key=lambda c: c.get("comment_text", ""),
                               threshold: float = 0.85) -> list[dict]:
    """When comments are re-extracted (or an AI review is re-run), carry the
    author's work -- status, notes, drafted responses, linked patches -- and
    the old id (so chat threads and patches stay linked) over to the matching
    new item."""
    used_old: set[int] = set()
    taken_ids = set()
    result = []
    for new in new_items:
        best, best_score = None, 0.0
        for i, old in enumerate(old_items):
            if i in used_old:
                continue
            s = _similarity(key(old), key(new))
            if s > best_score:
                best, best_score = i, s
        if best is not None and best_score >= threshold:
            used_old.add(best)
            old = old_items[best]
            for f in USER_FIELDS:
                if f in ("triage",) and best_score < 0.97:
                    continue    # triage must be recomputed if the text changed
                if old.get(f) not in (None, "", [], "unaddressed"):
                    new[f] = old[f]
            if old["id"] not in taken_ids:
                new["id"] = old["id"]
        taken_ids.add(new["id"])
        result.append(new)
    # guarantee unique ids
    seen: dict[str, int] = {}
    for item in result:
        if item["id"] in seen:
            seen[item["id"]] += 1
            item["id"] = f"{item['id']}-{seen[item['id']]}"
        else:
            seen[item["id"]] = 0
    return result


def replace_source_comments(project, source_label: str, items: list[dict]) -> None:
    """Swap in freshly extracted comments for one source, preserving user work."""
    with project.mutate():
        old = [c for c in project.state["comments"] if c.get("source") == source_label]
        others = [c for c in project.state["comments"] if c.get("source") != source_label]
        merged = merge_preserving_user_work(old, items)
        other_ids = {c["id"] for c in others}
        for c in merged:
            if c["id"] in other_ids:
                c["id"] = f"{c['id']}-b"
        # keep reviewer comments before AI-review items
        reviewer = [c for c in others if c.get("origin", "reviewer") == "reviewer"]
        ai = [c for c in others if c.get("origin", "reviewer") != "reviewer"]
        project.state["comments"] = reviewer + merged + ai
