"""Reviewers write "line 123" and "p. 5" about the journal's line-numbered PDF,
not about your .tex/.docx. Given that submitted PDF, this reads its margin
line numbers and attaches the actual text of every line/page a comment
refers to -- so neither you nor the model has to go hunting."""
from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PDFTOTEXT = shutil.which("pdftotext")

_NUMBERED_RE = re.compile(r"^\s{0,12}(\d{1,5})\s+(?:(\d{1,5})\s+)?(\S.*)$")
_LINE_REF_RE = re.compile(
    r"\b(?:lines?|ll?\.|L(?=\s?\d))\s*(\d{1,5})(?:\s*(?:-|–|—|to|through)\s*(?:lines?\s*)?(\d{1,5}))?", re.I)
_PAGE_REF_RE = re.compile(r"\b(?:page|pages|p\.|pp\.|pg\.?)\s*(\d{1,3})(?:\s*(?:-|–)\s*(\d{1,3}))?", re.I)
_MAX_LINES_PER_REF = 15


@dataclass
class NumberedPDF:
    path: str
    lines: dict[int, str] = field(default_factory=dict)          # global line numbers
    page_lines: dict[tuple[int, int], str] = field(default_factory=dict)   # (page, line) when numbering restarts
    line_page: dict[int, int] = field(default_factory=dict)
    pages: dict[int, str] = field(default_factory=dict)
    per_page: bool = False

    @property
    def usable(self) -> bool:
        return len(self.lines) >= 15 or len(self.page_lines) >= 15


_cache: dict[tuple, NumberedPDF | None] = {}


def parse_numbered_pdf(path: str | Path) -> NumberedPDF | None:
    """None if the PDF can't be read or has no margin line numbers."""
    p = Path(path)
    if not p.is_file() or not PDFTOTEXT:
        return None
    key = (str(p), p.stat().st_mtime)
    if key in _cache:
        return _cache[key]
    try:
        r = subprocess.run([PDFTOTEXT, "-layout", "-enc", "UTF-8", str(p), "-"], capture_output=True,
                           timeout=120, text=True, encoding="utf-8", errors="replace")
        text = r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    result = _parse(text, str(p)) if text else None
    _cache[key] = result
    return result


def _parse(text: str, path: str) -> NumberedPDF | None:
    doc = NumberedPDF(path)
    rows: list[tuple[int, int | None, str]] = []          # (page, detected number, content)
    expected = 1
    for page_no, page in enumerate(text.split("\f"), 1):
        doc.pages[page_no] = page
        for raw in page.splitlines():
            m = _NUMBERED_RE.match(raw)
            num, content = None, raw.strip()
            if m:
                a, b, rest = int(m.group(1)), (int(m.group(2)) if m.group(2) else None), m.group(3)
                if expected <= a <= expected + 12:
                    num, content = a, (f"{m.group(2)} " if b is not None else "") + rest
                elif b is not None and expected <= b <= expected + 12:
                    num, content = b, rest                      # "2 14 Methods": section number first
                elif a <= 5 and rows and rows[-1][0] != page_no and rows[-1][1]:
                    num, content = a, (f"{m.group(2)} " if b is not None else "") + rest   # restarts per page
                    doc.per_page = True
            if num is not None:
                expected = num + 1
            if content:
                rows.append((page_no, num, content.strip()))
    nums = [n for _, n, _ in rows if n is not None]
    if len(nums) < 5:
        return None
    diffs = Counter(b - a for a, b in zip(nums, nums[1:]) if b > a)
    step = diffs.most_common(1)[0][0] if diffs else 1
    if step > 1:
        # lines before the first printed number (e.g. 1-4 when only 5, 10, ... are printed)
        first = next(i for i, r in enumerate(rows) if r[1] is not None)
        n0 = rows[first][1]
        for back, i in enumerate(range(first - 1, max(-1, first - n0), -1), 1):
            if rows[i][0] == rows[first][0]:
                rows[i] = (rows[i][0], n0 - back, rows[i][2])
    counter = None
    for page_no, num, content in rows:
        if num is not None:
            counter = num
        elif step > 1 and counter is not None:
            counter += 1            # numbering printed every `step` lines: fill in the rest
        else:
            continue
        if doc.per_page:
            doc.page_lines[(page_no, counter)] = content
        else:
            doc.lines.setdefault(counter, content)
            doc.line_page.setdefault(counter, page_no)
    return doc if doc.usable else None


def _join(lines: list[str]) -> str:
    out = ""
    for ln in lines:
        if out.endswith("-") and ln[:1].islower():
            out = out[:-1] + ln                # de-hyphenate "sig-" + "nificantly"
        else:
            out += (" " if out else "") + ln
    return " ".join(out.split())


def resolve_refs(comment_text: str, doc: NumberedPDF | None) -> list[dict]:
    """[{"ref": "lines 38-39", "page": 2, "text": "..."}] for each line (or
    page) reference in the comment that the PDF can answer."""
    if doc is None:
        return []
    out = []
    page_hint = None
    pm = _PAGE_REF_RE.search(comment_text)
    if pm:
        page_hint = int(pm.group(1))
    for m in _LINE_REF_RE.finditer(comment_text):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if b < a:
            b = a + (b if b < 10 else 0)       # "lines 120-5"
        b = min(b, a + _MAX_LINES_PER_REF - 1)
        if doc.per_page:
            if page_hint is None:
                continue
            got = [doc.page_lines.get((page_hint, n)) for n in range(a, b + 1)]
            page = page_hint
        else:
            got = [doc.lines.get(n) for n in range(a, b + 1)]
            page = doc.line_page.get(a)
        got = [g for g in got if g]
        if not got:
            continue
        label = f"line {a}" if a == b else f"lines {a}–{b}"
        if any(o["ref"] == label for o in out):
            continue
        out.append({"ref": label, "page": page, "text": _join(got)})
    if not out and pm:
        page = int(pm.group(1))
        body = " ".join(doc.pages.get(page, "").split())
        if body:
            out.append({"ref": f"page {page}", "page": page, "text": body[:700] + ("…" if len(body) > 700 else "")})
    return out


def annotate_comments(comments: list[dict], submitted_pdf: str | None) -> int:
    """Attach resolved_refs to every reviewer comment in place. Returns how many
    comments got at least one resolved reference."""
    doc = parse_numbered_pdf(submitted_pdf) if submitted_pdf else None
    n = 0
    for c in comments:
        if c.get("origin", "reviewer") != "reviewer":
            continue
        # the extraction model often moves "Lines 38-39:" out of the text into location_ref
        refs = resolve_refs(f"{c.get('location_ref') or ''} {c.get('comment_text', '')}", doc)
        c["resolved_refs"] = refs
        n += bool(refs)
    return n


def refs_block(comment: dict) -> str:
    refs = comment.get("resolved_refs") or []
    if not refs:
        return ""
    return "\n".join(f"  (The reviewer's “{r['ref']}” refers to this text in the submitted PDF"
                     f"{', page ' + str(r['page']) if r.get('page') else ''}: “{r['text']}”)" for r in refs)
