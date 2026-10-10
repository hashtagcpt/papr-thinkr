"""Splitting manuscript text into sections ("chunks"), finding the sections
relevant to a comment, and fitting a manuscript into a model's context."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from .documents import balanced_braces


@dataclass
class Chunk:
    id: str
    heading: str
    text: str
    start_line: int
    end_line: int
    level: int = 1
    kind: str = "body"          # body | preamble | references | abstract
    tokens: Counter = field(default_factory=Counter, repr=False)


_TEX_HEADING_RE = re.compile(r"^\s*\\(chapter|section|subsection|subsubsection)\*?\s*(\[[^\]]*\])?\s*\{")

_TEX_LEVELS = {"chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3}

_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")


_KNOWN_SECTIONS = (
    "abstract", "summary", "introduction", "background", "methods", "materials and methods",
    "methodology", "experimental procedures", "experimental design", "results",
    "results and discussion", "discussion", "conclusion", "conclusions", "limitations",
    "references", "bibliography", "literature cited", "acknowledgments", "acknowledgements",
    "supplementary material", "supplementary information", "appendix", "data availability",
    "code availability", "author contributions", "funding", "competing interests",
    "conflict of interest", "conflicts of interest", "ethics statement", "related work",
    "significance", "keywords", "figure legends", "figure captions", "statistical analysis",
)


_PLAIN_HEADING_RE = re.compile(r"^(\d{1,2}(\.\d{1,2}){0,2}\.?\s+)?([A-Z][A-Za-z0-9 ,:&/()'-]{2,70})$")

_REFERENCE_HEADINGS = ("references", "bibliography", "literature cited", "works cited")


def _plain_heading(line: str) -> tuple[int, str] | None:
    s = line.strip().strip("*").strip()
    if not s or len(s) > 80 or s.endswith((".", ",", ";")) and not re.match(r"^\d", s):
        return None
    m = _PLAIN_HEADING_RE.match(s)
    if not m:
        return None
    title = m.group(3).strip()
    numbered = bool(m.group(1))
    level = 1 + m.group(1).strip().rstrip(".").count(".") if numbered else 1
    if title.lower().rstrip(":") in _KNOWN_SECTIONS:
        return level, title.rstrip(":")
    if numbered and len(title.split()) <= 10 and not re.search(r"\b(is|are|was|were)\b", title):
        return level, title
    return None


def _tokenize(text: str) -> Counter:
    return Counter(re.findall(r"[a-z][a-z0-9]{3,}", text.lower()))


def chunk_manuscript(text: str, file_ext: str) -> list[Chunk]:
    """Split manuscript text into labeled chunks (by heading) with stable ids.
    Falls back to paragraph windows for text without recognisable headings."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []   # (line_index, level, heading_text)
    ext = (file_ext or "").lower()

    if ext == ".tex":
        for i, line in enumerate(lines):
            m = _TEX_HEADING_RE.match(line)
            if m:
                brace = line.find("{", m.end() - 1)
                title = balanced_braces(line, brace) or line[m.end():].rstrip("}")
                title = re.sub(r"\\[a-zA-Z]+\*?(\{([^}]*)\})?", r"\2", title).strip() or m.group(1)
                headings.append((i, _TEX_LEVELS[m.group(1)], title))
            elif re.match(r"^\s*\\begin\{abstract\}", line):
                headings.append((i, 1, "Abstract"))
            elif re.match(r"^\s*\\begin\{thebibliography\}|^\s*\\bibliography\{|^\s*\\printbibliography", line):
                headings.append((i, 1, "References"))
    else:
        in_fence = False
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _MD_HEADING_RE.match(line)
            if m:
                headings.append((i, len(m.group(1)), re.sub(r"[*_`]", "", m.group(2)).strip()))
                continue
            if ext not in (".md", ".markdown", ".rmd", ".qmd"):
                h = _plain_heading(line)
                if h and (i == 0 or not lines[i - 1].strip() or i + 1 < len(lines) and not lines[i + 1].strip()):
                    headings.append((i, h[0], h[1]))

    chunks: list[Chunk] = []
    if headings:
        if headings[0][0] > 0:
            pre = "\n".join(lines[0:headings[0][0]]).strip()
            if pre:
                chunks.append(Chunk("chunk-0-preamble", "(front matter)", pre, 0, headings[0][0],
                                    level=0, kind="preamble"))
        for idx, (line_no, level, heading) in enumerate(headings):
            end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
            body = "\n".join(lines[line_no:end]).strip()
            if not body:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:40] or f"section-{idx}"
            low = heading.lower().strip(": ")
            kind = ("references" if low in _REFERENCE_HEADINGS else
                    "abstract" if low in ("abstract", "summary") else "body")
            chunks.append(Chunk(f"chunk-{idx+1}-{slug}", heading, body, line_no, end, level, kind))
    else:
        # Paragraph windows of ~4000 chars, never splitting a paragraph.
        paras = re.split(r"\n\s*\n", text)
        buf: list[str] = []
        size = 0
        line_cursor = 0
        start_line = 0
        for para in paras:
            if size + len(para) > 4000 and buf:
                body = "\n\n".join(buf).strip()
                chunks.append(Chunk(f"chunk-{len(chunks)+1}-para", f"part {len(chunks)+1}",
                                    body, start_line, line_cursor))
                buf, size, start_line = [], 0, line_cursor
            buf.append(para)
            size += len(para)
            line_cursor += para.count("\n") + 2
        if buf and "\n\n".join(buf).strip():
            chunks.append(Chunk(f"chunk-{len(chunks)+1}-para", f"part {len(chunks)+1}",
                                "\n\n".join(buf).strip(), start_line, line_cursor))
    for c in chunks:
        c.tokens = _tokenize(c.text)
    return chunks


def split_text(text: str, max_chars: int) -> list[str]:
    """Split at paragraph (then line, then sentence) boundaries into pieces of
    at most max_chars. Never cuts mid-word unless a single line is enormous."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    buf = ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(buf) + len(para) <= max_chars:
            buf += para
            continue
        if buf.strip():
            pieces.append(buf)
        buf = ""
        if len(para) > max_chars:
            sub = re.split(r"(?<=[.!?])\s+|\n", para)
            for s in sub:
                if len(buf) + len(s) + 1 > max_chars and buf:
                    pieces.append(buf)
                    buf = ""
                while len(s) > max_chars:
                    pieces.append(s[:max_chars])
                    s = s[max_chars:]
                buf += s + " "
        else:
            buf = para
    if buf.strip():
        pieces.append(buf)
    return [p.strip() for p in pieces if p.strip()]


def review_units(chunks: list[Chunk], min_chars: int = 2500, max_chars: int = 30000) -> list[Chunk]:
    """Chunks sized for a section-by-section review pass: tiny adjacent
    sections are merged, huge ones split, references skipped."""
    units: list[Chunk] = []
    pending: Chunk | None = None
    for c in chunks:
        if c.kind == "references":
            continue
        if c.kind == "preamble" and len(c.text) < 300:
            continue
        if pending is None:
            pending = Chunk(c.id, c.heading, c.text, c.start_line, c.end_line, c.level, c.kind)
        elif len(pending.text) < min_chars and len(pending.text) + len(c.text) <= max_chars:
            pending.heading = f"{pending.heading} / {c.heading}"
            pending.text += "\n\n" + c.text
            pending.end_line = c.end_line
        else:
            units.append(pending)
            pending = Chunk(c.id, c.heading, c.text, c.start_line, c.end_line, c.level, c.kind)
    if pending is not None:
        units.append(pending)
    out: list[Chunk] = []
    for u in units:
        parts = split_text(u.text, max_chars)
        if len(parts) == 1:
            out.append(u)
        else:
            for i, part in enumerate(parts, 1):
                out.append(Chunk(f"{u.id}-p{i}", f"{u.heading} (part {i}/{len(parts)})", part,
                                 u.start_line, u.end_line, u.level, u.kind))
    for c in out:
        c.tokens = _tokenize(c.text)
    return out


def outline(chunks: list[Chunk]) -> str:
    rows = []
    for c in chunks:
        indent = "  " * max(0, c.level - 1)
        rows.append(f"{indent}- {c.heading} (~{len(c.text.split())} words)")
    return "\n".join(rows)


_LOC_FIG_RE = re.compile(r"\b(fig(?:ure)?s?\.?|table|eq(?:uation)?\.?|supp\w*\s+fig\w*)\s*(S?\d+[a-z]?)", re.I)

_LOC_LINE_RE = re.compile(r"\blines?\s+(\d+)", re.I)


def find_chunk_for_query(chunks: list[Chunk], query: str, max_chunks: int = 3) -> list[Chunk]:
    """Small lexical retrieval (TF-IDF-ish) with boosts for explicit section,
    figure/table and line references in the query."""
    if not chunks:
        return []
    q_tokens = _tokenize(query)
    n = len(chunks)
    df: Counter = Counter()
    for c in chunks:
        if not c.tokens:
            c.tokens = _tokenize(c.text)
        df.update(set(c.tokens))
    q_lower = query.lower()
    figs = [(m.group(1).lower()[:3], m.group(2).lower()) for m in _LOC_FIG_RE.finditer(query)]
    lines_ref = [int(m.group(1)) for m in _LOC_LINE_RE.finditer(query)]
    scored = []
    for c in chunks:
        score = 0.0
        for tok, qn in q_tokens.items():
            tf = c.tokens.get(tok, 0)
            if tf:
                idf = math.log(1 + n / (1 + df[tok]))
                score += idf * (1 + math.log(tf)) * min(qn, 3)
        if c.heading and c.heading.lower() in q_lower and len(c.heading) > 3:
            score += 8
        low = c.text.lower()
        for kind, num in figs:
            if re.search(rf"\b{kind}\w*\.?\s*{re.escape(num)}\b", low):
                score += 5
        for ln in lines_ref:
            if c.start_line <= ln <= c.end_line:
                score += 4
        if c.kind == "references":
            score *= 0.2
        scored.append((score, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = [c for s, c in scored if s > 0][:max_chunks]
    return top if top else [c for c in chunks if c.kind != "references"][:max_chunks]


def fit_text(text: str, chunks: list[Chunk], budget: int, focus_query: str | None = None) -> tuple[str, str | None]:
    """Return (text, note) where text fits in `budget` characters. Strategy:
    whole text -> drop references -> (with a focus query) most relevant
    sections in document order -> proportional truncation of every section."""
    if len(text) <= budget:
        return text, None
    body = [c for c in chunks if c.kind != "references"]
    joined = "\n\n".join(c.text for c in body)
    if body and len(joined) <= budget:
        return joined, "References section omitted to fit the model's context."
    if focus_query and body:
        ranked = find_chunk_for_query(body, focus_query, max_chunks=len(body))
        chosen: list[Chunk] = []
        used = 0
        first = body[0]
        if len(first.text) < budget * 0.25:
            chosen.append(first)
            used += len(first.text)
        for c in ranked:
            if c in chosen:
                continue
            if used + len(c.text) > budget:
                continue
            chosen.append(c)
            used += len(c.text) + 2
        if chosen:
            chosen.sort(key=lambda c: body.index(c))
            return ("\n\n[...]\n\n".join(c.text for c in chosen),
                    "Only the sections most relevant to this task were included (manuscript exceeds the model's context).")
    if not body:
        return text[:budget], "Text truncated to fit the model's context."
    total = sum(len(c.text) for c in body)
    parts = []
    for c in body:
        share = max(600, int(budget * len(c.text) / total) - 40)
        if len(c.text) <= share:
            parts.append(c.text)
        else:
            parts.append(c.text[:share].rsplit(" ", 1)[0] + "\n[... section truncated ...]")
    out = "\n\n".join(parts)
    return out[:budget], "Every section was truncated proportionally to fit the model's context; prefer a model with a larger context window for a complete review."
