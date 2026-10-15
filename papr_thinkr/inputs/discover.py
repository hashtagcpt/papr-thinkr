"""Point papr-thinkr at a paper's folder and it works out what's what: the
main manuscript, the reviewer/decision letters, drafts carrying co-authors'
Word margin comments, the line-numbered submitted PDF, and the code, data
and figures behind the paper.

Real project folders are messy -- old drafts, IRB forms, literature PDFs,
backups, other people's toolboxes -- so manuscripts are chosen by their
*content* (length, section headings, figure captions), reviews must look like
reviews inside, and backups/vendored code/archives are skipped. Everything it
finds is a suggestion to correct before the project is created."""
from __future__ import annotations

import math
import re
import zipfile
from collections import Counter
from pathlib import Path

from .code_and_data import CODE_EXTS, SKIP_DIRS
from .documents import flatten_tex, read_text_file
from .figures import IMAGE_EXTS
from .line_refs import parse_numbered_pdf

_MAX_FILES = 25_000
_SKIP_DIR_RE = re.compile(r"(^|[-_ .])(backups?|old|archived?|deprecated|trash|tmp|temp|duplicates?|copy|"
                          r"unedited|attic|scratch)([-_ .]|\d|$)|-master$|^__MACOSX$", re.I)
_EDITOR_BACKUP_RE = re.compile(r"(~|\.asv|\.bak|\.orig|\.swp)$", re.I)
_NOT_MANUSCRIPT = re.compile(
    r"suppl(ement)?(?![a-z])|supplementary|supporting[-_ ]info|appendix|response|rebuttal|cover[-_ ]?letter|"
    r"\bletter\b|review|highlights|title[-_ ]?page|graphical|template|form\b|request|approval|closure|"
    r"questionnaire|consent|signature|docusign|invoice|receipt|poster|presentation|slides|\bcv\b|"
    r"progress[-_ ]report|budget|abstract|conflicted copy|^~\$", re.I)
_MANUSCRIPT_NAME = re.compile(r"manuscript|paper|article|draft|\bms\b|submission|thesis|dissertation|main", re.I)
_REVIEW_NAME = re.compile(r"review(er)?s?[-_ ]?(comments?|report|letter)?|referee|decision|editor'?s?[-_ ]?"
                          r"(comments?|letter|decision)|comments?[-_ ](from|to)[-_ ]", re.I)
_OWN_RESPONSE = re.compile(r"response|rebuttal|reply|cover[-_ ]?letter|point[-_ ]?by[-_ ]?point", re.I)
_REVIEW_CONTENT = re.compile(r"\breviewer\s*#?\s*\d|\breferee\b|comments? (for|to) the authors?|decision on (your )?manuscript|"
                             r"\bdear (dr|prof)\.?.{0,200}(manuscript|submission)|recommendation:\s*(minor|major|reject|accept)", re.I | re.S)
_KNOWN_HEADINGS = re.compile(r"^\s*(\d+(\.\d+)*\.?\s*)?(abstract|introduction|background|methods?|materials and methods|"
                             r"results|discussion|conclusions?|references|bibliography|general discussion|"
                             r"participants|stimuli|procedure|statistical analysis)\s*:?\s*\.?$", re.I)
_CAPTION = re.compile(r"^\s*(figure|fig\.?|table)\s*\d+", re.I)
_DATA_EXTS = {".csv", ".tsv", ".tab", ".dat", ".json", ".jsonl", ".xlsx", ".xls", ".mat", ".parquet", ".feather",
              ".h5", ".hdf5", ".rds", ".rdata", ".rda", ".sav", ".dta", ".npy", ".npz", ".nc"}
_DATA_LITERAL = re.compile(r"""["']([^"'\n]{1,200}\.(?:csv|tsv|xlsx|xls|json|mat|rds|rdata|parquet|txt|dat|h5))["']""", re.I)


def _walk(root: Path, max_depth: int = 6):
    """Files under root, skipping hidden dirs, backups, GitHub downloads and
    third-party toolboxes (a sub-folder with its own LICENSE + README)."""
    skipped: list[str] = []
    stack = [(root, 0)]
    n = 0
    while stack:
        d, depth = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            continue
        names = {p.name.lower() for p in entries}
        if d != root and any(x.startswith(("license", "licence", "copying")) for x in names) \
                and any(x.startswith("readme") for x in names):
            skipped.append(f"{d.relative_to(root)} (third-party package)")
            continue
        for p in entries:
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            if p.is_dir():
                if _SKIP_DIR_RE.search(p.name):
                    skipped.append(f"{p.relative_to(root)} (backup/old copy)")
                elif depth < max_depth:
                    stack.append((p, depth + 1))
            elif not _EDITOR_BACKUP_RE.search(p.name):
                n += 1
                if n > _MAX_FILES:
                    return
                yield p
    _walk.skipped = skipped  # type: ignore[attr-defined]


def _docx_profile(p: Path) -> dict:
    """Word count, recognised section headings, captions and margin comments
    of a .docx -- read straight from the zip, without pandoc."""
    try:
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            comments = z.read("word/comments.xml").decode("utf-8", "replace") if "word/comments.xml" in z.namelist() else ""
    except (zipfile.BadZipFile, KeyError, OSError):
        return {"words": 0, "headings": 0, "captions": 0, "comments": 0, "comment_authors": {}}
    paras = [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
    text = " ".join(paras)
    authors = Counter(re.findall(r'<w:comment [^>]*w:author="([^"]*)"', comments))
    return {"words": len(text.split()),
            "headings": len({h.strip().lower() for h in paras if _KNOWN_HEADINGS.match(h)}),
            "captions": sum(1 for h in paras if _CAPTION.match(h)),
            "comments": sum(authors.values()), "comment_authors": dict(authors)}


def _tex_profile(p: Path) -> dict:
    try:
        text = read_text_file(p)[0]
    except Exception:
        return {"words": 0, "headings": 0, "captions": 0}
    if "\\documentclass" not in text[:20000]:
        return {"words": 0, "headings": 0, "captions": 0}
    # A master file often contains only the preamble and several \input calls.
    text = flatten_tex(p)
    body = re.sub(r"\\[a-zA-Z]+|[{}$]", " ", text)
    return {"words": len(body.split()), "headings": len(re.findall(r"\\(?:sub)*section\*?\{", text)),
            "captions": text.count("\\caption")}


def _manuscript_score(p: Path, prof: dict) -> float:
    short_paper = prof["words"] >= 150 and prof["headings"] >= 2 and _MANUSCRIPT_NAME.search(p.stem)
    if prof["words"] < 800 and not short_paper:
        return -1
    score = 4 * math.log10(prof["words"]) + 2 * min(prof["headings"], 6) + min(prof["captions"], 10) * 0.5
    if _MANUSCRIPT_NAME.search(p.stem):
        score += 3
    if _NOT_MANUSCRIPT.search(p.name):
        score -= 15
    if prof.get("comments"):
        score -= 2            # a commented copy is feedback on a draft, not the latest draft itself
    return score


def _looks_like_review(p: Path) -> bool:
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            with zipfile.ZipFile(p) as z:
                head = re.sub(r"<[^>]+>", " ", z.read("word/document.xml").decode("utf-8", "replace"))[:8000]
        elif ext == ".pdf":
            from .line_refs import PDFTOTEXT
            import subprocess
            if not PDFTOTEXT:
                return True
            head = subprocess.run([PDFTOTEXT, "-l", "2", "-enc", "UTF-8", str(p), "-"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=60).stdout[:8000]
        else:
            head = read_text_file(p)[0][:8000]
    except Exception:
        return False
    return bool(_REVIEW_CONTENT.search(head))


def discover(folder: str | Path) -> dict:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {folder}")
    _walk.skipped = []  # type: ignore[attr-defined]
    files = list(_walk(root))
    skipped = list(getattr(_walk, "skipped", []))
    notes: list[str] = []

    # --- manuscript (by content) ----------------------------------------------
    scored = []
    feedback = []
    for p in files:
        ext = p.suffix.lower()
        if ext == ".docx" and not p.name.startswith("~$"):
            prof = _docx_profile(p)
        elif ext == ".tex":
            prof = _tex_profile(p)
        elif ext in (".md", ".rmd", ".qmd") and _MANUSCRIPT_NAME.search(p.stem):
            text = read_text_file(p)[0]
            prof = {"words": len(text.split()), "headings": len(re.findall(r"^#+ ", text, re.M)), "captions": 0}
        else:
            continue
        if prof.get("comments"):
            feedback.append({"path": str(p), "comments": prof["comments"], "authors": prof["comment_authors"],
                             "modified": p.stat().st_mtime})
        s = _manuscript_score(p, prof)
        if s > 0:
            scored.append((s, p.stat().st_mtime, p, prof))
    # Among genuine manuscripts (within a few points of the best), the newest wins.
    if scored:
        best = max(s for s, *_ in scored)
        top = [t for t in scored if t[0] >= best - 4]
        top.sort(key=lambda t: -t[1])
        rest = sorted((t for t in scored if t not in top), key=lambda t: -t[0])
        ordered = top + rest
    else:
        ordered = []
    candidates = [{"path": str(p), "words": prof["words"], "headings": prof["headings"],
                   "captions": prof["captions"], "modified": mtime} for _s, mtime, p, prof in ordered]
    manuscript = candidates[0]["path"] if candidates else None
    if len(candidates) > 1:
        notes.append(f"{len(candidates)} documents look like manuscripts; picked the most recent full draft "
                     f"({Path(manuscript).name}). Change it if that's not the one.")

    # --- reviews (name AND content must agree) ----------------------------------
    reviews, own = [], []
    for p in files:
        if p.suffix.lower() not in (".docx", ".pdf", ".txt", ".md", ".rtf", ".odt", ".eml"):
            continue
        if str(p) == manuscript or any(f["path"] == str(p) for f in feedback):
            continue
        if _OWN_RESPONSE.search(p.stem) and not re.search(r"decision|referee|reviewer[-_ ]?comments", p.stem, re.I):
            if _REVIEW_NAME.search(p.stem):
                own.append(str(p))
            continue
        if (_REVIEW_NAME.search(p.stem) or _REVIEW_NAME.search(p.parent.name)) and _looks_like_review(p):
            reviews.append(str(p))
    if own:
        notes.append(f"Ignored {len(own)} file(s) that look like your own response letter(s): "
                     + ", ".join(Path(o).name for o in own[:4]))
    if feedback:
        feedback.sort(key=lambda f: -f["modified"])
        notes.append(f"{len(feedback)} Word file(s) carry margin comments from co-authors/advisors; add them as "
                     f"reviewer comments to have every margin comment triaged.")

    # --- submitted PDF with line numbers ------------------------------------------
    submitted = None
    pdfs = [p for p in files if p.suffix.lower() == ".pdf" and str(p) not in reviews
            and re.search(r"submi|proof|manuscript|main|merged|draft|review[-_ ]?copy|\bms\b", p.stem, re.I)]
    for p in sorted(pdfs, key=lambda p: -p.stat().st_mtime)[:12]:
        if parse_numbered_pdf(p):
            submitted = str(p)
            break

    # --- code ------------------------------------------------------------------
    code_files = [p for p in files if p.suffix.lower() in CODE_EXTS and p.suffix.lower() != ".bat"]
    code_paths = _folders(code_files, root)

    # --- data: prefer what the code actually reads ---------------------------------
    referenced = Counter()
    for p in code_files:
        if p.stat().st_size > 2_000_000:
            continue
        try:
            text = read_text_file(p)[0]
        except Exception:
            continue
        for m in _DATA_LITERAL.finditer(text):
            referenced[Path(m.group(1).replace("\\", "/")).name.lower()] += 1
    data_files = [p for p in files if p.suffix.lower() in _DATA_EXTS]
    used = [p for p in data_files if p.name.lower() in referenced]
    if used:
        data_paths = sorted({str(p) for p in used})[:40]
        unread = len(data_files) - len(used)
        if unread:
            notes.append(f"Data: picked the {len(used)} file(s) your code reads; {unread} other data file(s) "
                         f"were left out (add folders if they matter).")
    else:
        data_paths = _folders(data_files, root)
    missing = sorted(n for n in referenced if not any(p.name.lower() == n for p in data_files))
    if missing:
        notes.append(f"The code reads {len(missing)} data file(s) not found in this folder: "
                     + ", ".join(missing[:8]) + ("..." if len(missing) > 8 else ""))

    # --- figures -----------------------------------------------------------------
    figure_dirs = Counter(p.parent for p in files if p.suffix.lower() in IMAGE_EXTS
                          and re.search(r"fig|plot|img|image|graph", str(p.relative_to(root)), re.I))
    figures_dir = str(figure_dirs.most_common(1)[0][0]) if figure_dirs else None
    if skipped:
        notes.append(f"Skipped {len(skipped)} folder(s): " + ", ".join(skipped[:6]) + ("..." if len(skipped) > 6 else ""))

    return {
        "folder": str(root),
        "manuscript": manuscript,
        "manuscript_candidates": candidates[:8],
        "reviews": reviews,
        "commented_drafts": [{k: v for k, v in f.items() if k != "modified"} for f in feedback][:10],
        "submitted_pdf": submitted,
        "code_paths": code_paths,
        "data_paths": data_paths,
        "figures_dir": figures_dir,
        "summary": {"files_scanned": len(files),
                    "code_languages": dict(Counter(p.suffix.lower() for p in code_files)),
                    "data_formats": dict(Counter(p.suffix.lower() for p in data_files)),
                    "figure_files": sum(1 for p in files if p.suffix.lower() in IMAGE_EXTS),
                    "skipped_folders": skipped[:30]},
        "notes": notes,
    }


def _folders(paths: list[Path], root: Path) -> list[str]:
    """Collapse files into the smallest set of folders (plus loose top-level files)."""
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
    return [str(c) for c in chosen][:15] + loose[:40]
