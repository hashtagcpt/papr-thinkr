"""Critique findings: normalising what a model returns, checking that quoted
text really is in the manuscript, and merging duplicates raised by several
referees."""
from __future__ import annotations

import re

from .. import prompts
from .effort import clamp01, normalize_effort


SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "editorial": 3, "unclear": 4}


_STOP = set("""the and for with that this from are was were has have had not but its their they them
which into than then there these those such also been being more most very can may might should would
could will does did using used use paper manuscript authors author study results data section figure
table text line lines""".split())


def norm_text(s: str) -> str:
    s = re.sub(r"\\[a-zA-Z]+\*?", " ", s or "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def verify_quote(quote: str | None, haystack_norm: str) -> bool | None:
    """Is the reviewer's 'verbatim' quote really in the source? None if no quote."""
    if not quote:
        return None
    q = norm_text(quote)
    if not q:
        return None
    if q in haystack_norm:
        return True
    words = q.split()
    if len(words) < 6:
        return False
    windows = [" ".join(words[i:i + 6]) for i in range(0, len(words) - 5, 3)]
    hits = sum(1 for w in windows if w in haystack_norm)
    return hits >= max(1, len(windows) // 2)


def normalize_item(raw: dict, persona: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = " ".join(str(raw.get("title") or "").split())[:160]
    issue = str(raw.get("issue") or "").strip()
    if not issue and not title:
        return None
    severity = raw.get("severity") if raw.get("severity") in prompts.SEVERITIES else "minor"
    category = raw.get("category") if raw.get("category") in prompts.CATEGORIES else "other"
    quote = raw.get("quote")
    quote = " ".join(str(quote).split()).strip("\"'\u201c\u201d ")[:500] if quote else None
    location = raw.get("location")
    location = str(location).strip()[:160] if location not in (None, "", "null") else None
    return {
        "title": title or issue[:90],
        "category": category,
        "severity": severity,
        "location": location,
        "quote": quote or None,
        "issue": issue,
        "rationale": str(raw.get("rationale") or "").strip(),
        "suggestion": str(raw.get("suggestion") or "").strip(),
        "effort": normalize_effort(raw),
        "confidence": round(clamp01(raw.get("confidence"), 0.6), 2),
        "personas": [raw.get("persona") or persona],
        "support": 1,
    }


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in _STOP}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _is_dup(a: dict, b: dict) -> bool:
    jt = _jaccard(_words(a["title"]), _words(b["title"]))
    if jt >= 0.6:
        return True
    ji = _jaccard(_words(a["issue"]), _words(b["issue"]))
    if a.get("quote") and b.get("quote"):
        qa, qb = norm_text(a["quote"]), norm_text(b["quote"])
        if qa and qb and (qa in qb or qb in qa) and (ji >= 0.2 or jt >= 0.3):
            return True
    return jt >= 0.4 and ji >= 0.35


def merge_group(group: list[dict]) -> dict:
    if len(group) == 1:
        return group[0]
    primary = min(group, key=lambda it: (SEVERITY_RANK[it["severity"]], -len(it["issue"])))
    merged = dict(primary)
    merged["severity"] = min((it["severity"] for it in group), key=lambda s: SEVERITY_RANK[s])
    personas: list[str] = []
    for it in group:
        for p in it["personas"]:
            if p not in personas:
                personas.append(p)
    merged["personas"] = personas
    merged["support"] = sum(it.get("support", 1) for it in group)
    top = max(it["confidence"] for it in group)
    # Several independent passes flagging the same problem raises confidence,
    # but they share a model, so only modestly.
    merged["confidence"] = round(min(0.97, top + (1 - top) * 0.25 * (len(group) - 1)), 2)
    for k in ("quote", "location", "suggestion", "rationale"):
        if not merged.get(k):
            merged[k] = next((it[k] for it in group if it.get(k)), merged.get(k))
    if merged.get("quote_verified") is not True:
        verified = [it for it in group if it.get("quote_verified")]
        if verified:
            merged["quote"] = verified[0]["quote"]
            merged["quote_verified"] = True
    eff = dict(primary["effort"])
    eff["hours_high"] = max(it["effort"]["hours_high"] for it in group)
    eff["needs_new_data"] = any(it["effort"]["needs_new_data"] for it in group)
    merged["effort"] = eff
    return merged


def dedupe(items: list[dict]) -> list[dict]:
    groups: list[list[dict]] = []
    for it in items:
        for g in groups:
            if any(_is_dup(it, other) for other in g):
                g.append(it)
                break
        else:
            groups.append([it])
    return [merge_group(g) for g in groups]
