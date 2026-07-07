"""Converts source documents (docx, pdf, tex, md, txt) into plain text,
and splits manuscript text into addressable chunks for LLM context."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PANDOC = shutil.which("pandoc")
PDFTOTEXT = shutil.which("pdftotext")


class IngestError(RuntimeError):
    pass


def extract_text(path: str) -> str:
    """Best-effort extraction of plain text from a source file, based on extension."""
    p = Path(path)
    if not p.exists():
        raise IngestError(f"File not found: {path}")
    ext = p.suffix.lower()

    if ext in (".txt", ".tex", ".md", ".bib"):
        return p.read_text(encoding="utf-8", errors="replace")

    if ext == ".docx":
        if not PANDOC:
            raise IngestError("pandoc is required to read .docx files but was not found on PATH.")
        result = subprocess.run(
            [PANDOC, str(p), "-t", "plain", "--wrap=none"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            raise IngestError(f"pandoc failed on {path}: {result.stderr}")
        return result.stdout

    if ext == ".pdf":
        if PDFTOTEXT:
            result = subprocess.run(
                [PDFTOTEXT, "-layout", str(p), "-"],
                capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            if result.returncode == 0:
                return result.stdout
        if PANDOC:
            result = subprocess.run(
                [PANDOC, str(p), "-t", "plain", "--wrap=none"],
                capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            if result.returncode == 0:
                return result.stdout
        raise IngestError(
            f"Could not extract text from {path}: neither pdftotext nor pandoc succeeded."
        )

    if ext in (".doc", ".rtf", ".odt"):
        if not PANDOC:
            raise IngestError(f"pandoc is required to read {ext} files but was not found on PATH.")
        result = subprocess.run(
            [PANDOC, str(p), "-t", "plain", "--wrap=none"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            raise IngestError(f"pandoc failed on {path}: {result.stderr}")
        return result.stdout

    # Fallback: try reading as text
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise IngestError(f"Unsupported file type '{ext}' and could not read as text: {e}")


@dataclass
class Chunk:
    id: str
    heading: str
    text: str
    start_line: int
    end_line: int


_TEX_HEADING_RE = re.compile(
    r"^\\(section|subsection|subsubsection)\*?\{(.+?)\}", re.MULTILINE
)
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)


def chunk_manuscript(text: str, file_ext: str) -> list[Chunk]:
    """Split manuscript text into labeled chunks (by heading) with stable ids.
    Falls back to paragraph-based chunking for formats without clear headings."""
    lines = text.splitlines()
    headings: list[tuple[int, str]] = []  # (line_index, heading_text)

    if file_ext == ".tex":
        for i, line in enumerate(lines):
            m = _TEX_HEADING_RE.match(line.strip())
            if m:
                headings.append((i, m.group(2).strip()))
    elif file_ext == ".md":
        for i, line in enumerate(lines):
            m = _MD_HEADING_RE.match(line)
            if m:
                headings.append((i, m.group(2).strip()))

    chunks: list[Chunk] = []
    if headings:
        # content before first heading
        if headings[0][0] > 0:
            pre = "\n".join(lines[0:headings[0][0]]).strip()
            if pre:
                chunks.append(Chunk("chunk-0-preamble", "(preamble)", pre, 0, headings[0][0]))
        for idx, (line_no, heading) in enumerate(headings):
            end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
            body = "\n".join(lines[line_no:end]).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:40] or f"section-{idx}"
            chunks.append(Chunk(f"chunk-{idx+1}-{slug}", heading, body, line_no, end))
    else:
        # paragraph-based fallback, grouped into ~40-line windows to keep chunks addressable
        window = 40
        for idx, start in enumerate(range(0, max(len(lines), 1), window)):
            end = min(start + window, len(lines))
            body = "\n".join(lines[start:end]).strip()
            if body:
                chunks.append(Chunk(f"chunk-{idx+1}-para", f"lines {start+1}-{end}", body, start, end))

    return chunks


def find_chunk_for_query(chunks: list[Chunk], query: str, max_chunks: int = 3) -> list[Chunk]:
    """Very small lexical retrieval: score chunks by overlapping significant words."""
    query_words = set(re.findall(r"[a-z]{4,}", query.lower()))
    if not query_words:
        return chunks[:max_chunks]
    scored = []
    for c in chunks:
        chunk_words = set(re.findall(r"[a-z]{4,}", c.text.lower()))
        score = len(query_words & chunk_words)
        scored.append((score, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = [c for score, c in scored if score > 0][:max_chunks]
    return top if top else chunks[:max_chunks]
