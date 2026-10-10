"""Point papr-thinkr at a paper's folder and it works out what's what: the
main manuscript, the reviewer/decision letters, the line-numbered submitted
PDF, and the folders holding code, data and figures. Everything it finds is
a suggestion you can correct before the project is created."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .code_and_data import CODE_EXTS, DATA_BINARY_EXTS, SKIP_DIRS
from .documents import read_text_file
from .figures import IMAGE_EXTS
from .line_refs import parse_numbered_pdf

_REVIEW_NAME = re.compile(r"review|referee|decision|reviewer|comments?|critique|report|editor|\bletter\b|"
                          r"revision[-_ ]?request|rev[-_ ]?\d|r\d[-_ ]?comments", re.I)
_OWN_RESPONSE = re.compile(r"response|rebuttal|reply|cover[-_ ]?letter|point[-_ ]?by[-_ ]?point", re.I)
_MANUSCRIPT_NAME = re.compile(r"main|manuscript|paper|article|draft|ms\b|submission", re.I)
_NOT_MANUSCRIPT = re.compile(r"supp|supplement|appendix|response|rebuttal|cover|letter|review|highlights|"
                             r"title[-_ ]?page|graphical|template|^~\$", re.I)
_SUBMITTED_NAME = re.compile(r"submi|proof|manuscript|main|merged|pdf[-_ ]?with|review[-_ ]?copy|ms\b", re.I)
_DATA_EXTS = DATA_BINARY_EXTS | {".csv", ".tsv", ".tab", ".dat", ".json", ".jsonl", ".xlsx", ".xls", ".mat"}
_MAX_FILES = 4000


def _walk(root: Path, max_depth: int = 5):
    stack = [(root, 0)]
    n = 0
    while stack:
        d, depth = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            continue
        for p in entries:
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            if p.is_dir():
                if depth < max_depth:
                    stack.append((p, depth + 1))
            else:
                n += 1
                if n > _MAX_FILES:
                    return
                yield p


def _tex_score(p: Path) -> int:
    try:
        head = read_text_file(p)[0][:20000]
    except Exception:
        return -1
    if "\\documentclass" not in head:
        return -1
    score = 10
    score += 5 if "\\begin{document}" in head else 0
    score += 4 if _MANUSCRIPT_NAME.search(p.stem) else 0
    score -= 8 if _NOT_MANUSCRIPT.search(p.stem) else 0
    score += min(5, head.count("\\section"))
    return score


def discover(folder: str | Path) -> dict:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {folder}")
    files = list(_walk(root))
    notes: list[str] = []

    # --- manuscript ---------------------------------------------------------
    tex = sorted(((s, p) for p in files if p.suffix.lower() == ".tex" for s in [_tex_score(p)] if s > 0),
                 key=lambda t: (-t[0], len(str(t[1]))))
    docx = [p for p in files if p.suffix.lower() == ".docx" and not p.name.startswith("~$")]
    docx_ms = sorted((p for p in docx if not _NOT_MANUSCRIPT.search(p.stem) and not _REVIEW_NAME.search(p.stem)),
                     key=lambda p: (-p.stat().st_mtime))
    candidates = [str(p) for _, p in tex] + [str(p) for p in docx_ms]
    candidates += [str(p) for p in files if p.suffix.lower() in (".md", ".rmd", ".qmd")
                   and _MANUSCRIPT_NAME.search(p.stem) and not _NOT_MANUSCRIPT.search(p.stem)]
    manuscript = candidates[0] if candidates else None
    if tex and docx_ms:
        notes.append("Found both LaTeX and Word manuscripts; picked the LaTeX one -- change it if that's wrong.")
    if docx_ms and len(docx_ms) > 1 and not tex:
        notes.append("Several Word files look like manuscripts; picked the most recently modified.")

    # --- reviews ------------------------------------------------------------
    review_exts = (".docx", ".pdf", ".txt", ".md", ".rtf", ".odt", ".eml")
    reviews, own = [], []
    for p in files:
        if p.suffix.lower() not in review_exts or str(p) == manuscript:
            continue
        if _OWN_RESPONSE.search(p.stem) and not re.search(r"decision|referee|reviewer[-_ ]?comments", p.stem, re.I):
            own.append(str(p))
        elif _REVIEW_NAME.search(p.stem) or _REVIEW_NAME.search(p.parent.name):
            reviews.append(str(p))
    if own:
        notes.append(f"Ignored {len(own)} file(s) that look like your own response letter(s): "
                     + ", ".join(Path(o).name for o in own[:4]))

    # --- submitted PDF with line numbers ---------------------------------------
    submitted = None
    pdfs = [p for p in files if p.suffix.lower() == ".pdf" and str(p) not in reviews]
    for p in sorted(pdfs, key=lambda p: (not _SUBMITTED_NAME.search(p.stem), -p.stat().st_mtime))[:12]:
        if parse_numbered_pdf(p):
            submitted = str(p)
            break
    if pdfs and not submitted:
        notes.append("No line-numbered PDF found; add the PDF the reviewers saw so 'line 123' references can be resolved.")

    # --- code, data, figures (as folders where possible) --------------------------
    def folders(paths: list[Path]) -> list[str]:
        if not paths:
            return []
        by_dir = Counter(p.parent for p in paths)
        chosen: list[Path] = []
        for d, _n in by_dir.most_common():
            if d == root:
                continue
            top = d
            while top.parent != root and top.parent != top and top.parent in by_dir:
                top = top.parent
            if not any(top == c or top.is_relative_to(c) for c in chosen):
                chosen = [c for c in chosen if not c.is_relative_to(top)] + [top]
        loose = [str(p) for p in paths if p.parent == root]
        return [str(c) for c in chosen][:12] + loose[:20]

    ms_dir = Path(manuscript).parent if manuscript else None
    code_files = [p for p in files if p.suffix.lower() in CODE_EXTS and p.suffix.lower() != ".bat"]
    data_files = [p for p in files if p.suffix.lower() in _DATA_EXTS and not (ms_dir and p.parent == ms_dir and
                                                                                p.suffix.lower() == ".json")]
    figure_files = [p for p in files if p.suffix.lower() in IMAGE_EXTS and p.suffix.lower() != ".pdf"
                    or (p.suffix.lower() == ".pdf" and re.search(r"fig", p.stem, re.I))]
    figure_dirs = sorted({str(p.parent) for p in figure_files if re.search(r"fig|plot|img|image|graph", p.parent.name, re.I)})
    langs = Counter(p.suffix.lower() for p in code_files)
    return {
        "folder": str(root),
        "manuscript": manuscript,
        "manuscript_candidates": candidates[:8],
        "reviews": reviews,
        "submitted_pdf": submitted,
        "code_paths": folders(code_files),
        "data_paths": folders(data_files),
        "figures_dir": figure_dirs[0] if figure_dirs else None,
        "summary": {"files_scanned": len(files), "code_languages": dict(langs),
                    "data_formats": dict(Counter(p.suffix.lower() for p in data_files)),
                    "figure_files": len(figure_files)},
        "notes": notes,
    }
