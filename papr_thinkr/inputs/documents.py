"""Reading manuscripts and letters: converts .docx/.pdf/.tex/.md/.txt (and
more) into text, reads and writes plain-text manuscripts without mangling
their encoding or line endings, and flattens multi-file LaTeX projects."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


PANDOC = shutil.which("pandoc")

PDFTOTEXT = shutil.which("pdftotext")


# Manuscript formats papr-thinkr can edit in place (find/replace patches).
EDITABLE_EXTS = {".tex", ".md", ".markdown", ".txt", ".rmd", ".qmd", ".rst", ".org"}


PANDOC_EXTS = {".docx", ".odt", ".rtf", ".epub", ".html", ".htm"}

_SUBPROCESS_TIMEOUT = 180


class IngestError(RuntimeError):
    pass


@dataclass
class TextMeta:
    encoding: str = "utf-8"
    newline: str = "\n"
    bom: bool = False


def decode_bytes(raw: bytes) -> tuple[str, TextMeta]:
    meta = TextMeta()
    if raw.startswith(b"\xef\xbb\xbf"):
        meta.bom = True
        raw = raw[3:]
    for enc in ("utf-8", "cp1252"):
        try:
            text = raw.decode(enc)
            meta.encoding = enc
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
        meta.encoding = "latin-1"
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    meta.newline = "\r\n" if crlf > lf else "\n"
    return text.replace("\r\n", "\n"), meta


def read_text_file(path: str | Path) -> tuple[str, TextMeta]:
    """Returns text with '\n' newlines plus what's needed to write it back
    byte-identically (encoding, original newline style, BOM)."""
    p = Path(path)
    if not p.exists():
        raise IngestError(f"File not found: {path}")
    return decode_bytes(p.read_bytes())


def write_text_file(path: str | Path, text: str, meta: TextMeta) -> None:
    out = text.replace("\r\n", "\n")
    if meta.newline != "\n":
        out = out.replace("\n", meta.newline)
    try:
        data = out.encode(meta.encoding)
    except UnicodeEncodeError as e:
        raise IngestError(
            f"The new text contains characters that cannot be saved in this file's "
            f"{meta.encoding} encoding ({e.reason}: {out[e.start:e.end]!r}). Edit the patch "
            f"to avoid them or convert the file to UTF-8.") from e
    if meta.bom:
        data = b"\xef\xbb\xbf" + data
    p = Path(path)
    tmp = p.with_name(p.name + ".papr-tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def is_editable(path: str | Path) -> bool:
    return Path(path).suffix.lower() in EDITABLE_EXTS


def _run(args: list[str], what: str) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=_SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise IngestError(f"{what} timed out after {_SUBPROCESS_TIMEOUT} s")
    except OSError as e:
        raise IngestError(f"{what} could not be started: {e}")
    if result.returncode != 0:
        raise IngestError(f"{what} failed: {result.stderr.strip()[:800]}")
    return result.stdout


def _pandoc_to_markdown(p: Path) -> str:
    if not PANDOC:
        raise IngestError(f"pandoc is required to read {p.suffix} files but was not found on PATH.")
    args = [PANDOC, str(p), "-t", "gfm", "--wrap=none"]
    if p.suffix.lower() == ".docx":
        args.append("--track-changes=accept")   # review the manuscript as revised
    return _run(args, f"pandoc on {p.name}")


def extract_text(path: str | Path) -> str:
    """Best-effort extraction of readable text from a source file. Word-like
    documents become Markdown (so headings and tables survive)."""
    p = Path(path)
    if not p.exists():
        raise IngestError(f"File not found: {path}")
    if p.is_dir():
        raise IngestError(f"Expected a file but got a directory: {path}")
    ext = p.suffix.lower()

    if ext in EDITABLE_EXTS or ext in (".bib", ".csv", ".tsv", ".json", ".log"):
        return read_text_file(p)[0]

    if ext in PANDOC_EXTS:
        return _pandoc_to_markdown(p)

    if ext == ".pdf":
        errors = []
        if PDFTOTEXT:
            # No -layout: in two-column papers it interleaves the columns line by line.
            try:
                return _run([PDFTOTEXT, "-enc", "UTF-8", str(p), "-"], f"pdftotext on {p.name}")
            except IngestError as e:
                errors.append(str(e))
        else:
            errors.append("pdftotext not found on PATH")
        raise IngestError(f"Could not extract text from {path}: {'; '.join(errors)}. "
                          f"Install poppler (pdftotext).")

    if ext == ".doc":
        raise IngestError(f"Legacy .doc files are not supported ({p.name}); save it as .docx first.")

    raw = p.read_bytes()
    if b"\x00" in raw[:4096]:
        raise IngestError(f"Unsupported binary file type '{ext}': {path}")
    return decode_bytes(raw)[0]


_TEX_INCLUDE_RE = re.compile(r"\\(input|include|subfile)\s*\{([^}]+)\}")

_TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)


def _strip_tex_comments(text: str) -> str:
    return _TEX_COMMENT_RE.sub("", text)


def tex_component_files(main: str | Path, _seen: set | None = None, _depth: int = 0) -> list[Path]:
    """Main .tex file followed by every file it (transitively) \\input's."""
    main = Path(main).resolve()
    seen = _seen if _seen is not None else set()
    if main in seen or _depth > 12 or not main.exists():
        return []
    seen.add(main)
    files = [main]
    text = _strip_tex_comments(read_text_file(main)[0])
    for m in _TEX_INCLUDE_RE.finditer(text):
        name = m.group(2).strip()
        cand = (main.parent / name)
        if cand.suffix.lower() != ".tex":
            cand = cand.with_name(cand.name + ".tex") if not cand.exists() else cand
        if cand.exists():
            files.extend(tex_component_files(cand, seen, _depth + 1))
    return files


def flatten_tex(main: str | Path, _depth: int = 0, _seen: set | None = None) -> str:
    main = Path(main).resolve()
    seen = _seen if _seen is not None else set()
    if main in seen or _depth > 12:
        return ""
    seen.add(main)
    text = read_text_file(main)[0]

    def repl(m: re.Match) -> str:
        # leave commented-out includes alone
        line_start = text.rfind("\n", 0, m.start()) + 1
        if "%" in text[line_start:m.start()].replace("\\%", ""):
            return m.group(0)
        name = m.group(2).strip()
        cand = main.parent / name
        if cand.suffix.lower() != ".tex" and not cand.exists():
            cand = cand.with_name(cand.name + ".tex")
        if not cand.exists():
            return m.group(0)
        return f"\n% ---- begin {cand.name} ----\n{flatten_tex(cand, _depth + 1, seen)}\n% ---- end {cand.name} ----\n"

    return _TEX_INCLUDE_RE.sub(repl, text)


def tex_body(text: str) -> str:
    """The reviewable part of a LaTeX document: title/author lines plus the
    document body (the package-loading preamble is noise for a reviewer)."""
    m = re.search(r"\\begin\{document\}", text)
    if not m:
        return text
    pre = text[:m.start()]
    keep = []
    for cmd in ("title", "author"):
        mm = re.search(r"\\" + cmd + r"\s*(\[[^\]]*\])?\s*\{", pre)
        if mm:
            arg = balanced_braces(pre, mm.end() - 1)
            if arg is not None:
                keep.append(f"\\{cmd}{{{arg}}}")
    end = re.search(r"\\end\{document\}", text)
    body = text[m.end():end.start() if end else len(text)]
    return ("\n".join(keep) + "\n" + body).strip()


def balanced_braces(s: str, open_idx: int) -> str | None:
    if open_idx >= len(s) or s[open_idx] != "{":
        return None
    depth = 0
    for i in range(open_idx, len(s)):
        c = s[i]
        if c == "\\":
            continue
        if c == "{" and (i == 0 or s[i - 1] != "\\"):
            depth += 1
        elif c == "}" and (i == 0 or s[i - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return s[open_idx + 1:i]
    return None


def read_manuscript(path: str | Path) -> str:
    """Full manuscript text for review/analysis purposes. Multi-file LaTeX is
    flattened; Word/PDF are converted to text."""
    p = Path(path)
    if not p.exists():
        raise IngestError(f"Manuscript file not found: {path}")
    if p.suffix.lower() == ".tex":
        return flatten_tex(p)
    return extract_text(p)


def manuscript_review_text(path: str | Path) -> str:
    text = read_manuscript(path)
    if Path(path).suffix.lower() == ".tex":
        text = tex_body(text)
    return text
