"""Figures: what the manuscript shows and the code that draws it.

For a .tex, .docx or .md manuscript this finds every figure (number, label,
caption, image files, the sentences that cite it), maps each one to the
script that saves it (R ggsave()/pdf()/png()..., Python savefig(), MATLAB
exportgraphics()/saveas()/print()), pulls out the plotting code and the data
it reads, and prepares a PNG of the figure for models that can see images."""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .documents import balanced_braces, flatten_tex, read_text_file

PDFTOPPM = shutil.which("pdftoppm")
GHOSTSCRIPT = shutil.which("gswin64c") or shutil.which("gs")
IMAGE_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".tif", ".tiff", ".svg", ".gif", ".bmp", ".emf",
              ".wmf", ".webp")
_MAX_MODEL_IMAGE = 4_500_000


@dataclass
class Figure:
    number: int
    caption: str = ""
    label: str | None = None
    files: list[str] = field(default_factory=list)         # as written in the manuscript
    resolved: list[str] = field(default_factory=list)      # existing image files
    missing: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)      # sentences citing the figure
    code: list[dict] = field(default_factory=list)          # [{file, line, call, target, segment, reads, match}]
    image: str | None = None                                # PNG/JPG prepared for a vision model
    image_note: str | None = None
    pixel_size: tuple[int, int] | None = None

    def as_dict(self) -> dict:
        return {"number": self.number, "caption": self.caption, "label": self.label, "files": self.files,
                "resolved": self.resolved, "missing": self.missing, "mentions": self.mentions[:6],
                "code": [{k: v for k, v in c.items() if k != "segment"} for c in self.code],
                "image": self.image, "image_note": self.image_note, "pixel_size": self.pixel_size}

    def describe(self, with_code: bool = True, code_chars: int = 6000) -> str:
        """Text block for a prompt."""
        lines = [f"FIGURE {self.number}" + (f" (label {self.label})" if self.label else "")]
        lines.append(f"Caption: {self.caption or '(no caption found)'}")
        if self.files:
            lines.append(f"Image file(s): {', '.join(self.files)}" +
                         (f" -- MISSING: {', '.join(self.missing)}" if self.missing else ""))
        if self.pixel_size:
            lines.append(f"Raster size: {self.pixel_size[0]}x{self.pixel_size[1]} px")
        for m in self.mentions[:5]:
            lines.append(f"Cited in text: \u201c{m}\u201d")
        if with_code:
            if not self.code:
                lines.append("Generating code: none found among the files provided.")
            for c in self.code:
                lines.append(f"Generating code ({c['match']}): {c['file']} line {c['line']}: {c['call']}")
                if c.get("reads"):
                    lines.append(f"  data read by this script: {', '.join(c['reads'])}")
                if c.get("segment"):
                    lines.append(c["segment"][:code_chars])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Finding figures in the manuscript
# ---------------------------------------------------------------------------

_FIG_ENV_RE = re.compile(r"\\begin\{(figure\*?|wrapfigure|SCfigure)\}(.*?)\\end\{\1\}", re.S)
_INCLUDE_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{((?:\{[^}]*\})+)\}")
_CAPTION_RE = re.compile(r"\\caption\s*(?:\[[^\]]*\])?\s*\{")
_LATEX_CMD_RE = re.compile(r"\\(?:textbf|textit|emph|mathrm|text|mathbf|textsc|underline)\{([^{}]*)\}")


def _clean_tex(s: str) -> str:
    s = re.sub(r"(?<!\\)%.*", "", s)
    for _ in range(3):
        s = _LATEX_CMD_RE.sub(r"\1", s)
    s = re.sub(r"\\(?:label|ref|cref|autoref)\{[^}]*\}", "", s)
    s = re.sub(r"\\pm(?![a-zA-Z])", "\u00b1", s)
    s = re.sub(r"\\mu(?![a-zA-Z])", "\u00b5", s)
    s = re.sub(r"\\(?:times)(?![a-zA-Z])", "\u00d7", s)
    s = s.replace("~", " ").replace("$", "")
    s = re.sub(r"\\[a-zA-Z]+\*?", "", s)
    return " ".join(s.replace("{", "").replace("}", "").split())


def _sentences_around(text: str, idx: int, width: int = 260) -> str:
    start = max(text.rfind(". ", 0, idx), text.rfind("\n\n", 0, idx), idx - width)
    end_candidates = [i for i in (text.find(". ", idx), text.find("\n\n", idx)) if i != -1]
    end = min(end_candidates + [idx + width])
    return " ".join(text[start + 1:end + 1].split())[:width * 2]


def _resolve_graphic(name: str, roots: list[Path]) -> Path | None:
    name = name.strip()
    for root in roots:
        cand = (root / name)
        if cand.suffix.lower() in IMAGE_EXTS and cand.is_file():
            return cand
        for ext in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".tif", ".tiff"):
            c2 = cand.with_name(cand.name + ext)
            if c2.is_file():
                return c2
    return None


def figures_from_tex(main: Path) -> list[Figure]:
    text = flatten_tex(main)
    body = text[text.find("\\begin{document}"):] if "\\begin{document}" in text else text
    roots = [main.parent]
    gp = _GRAPHICSPATH_RE.search(text)
    if gp:
        roots += [main.parent / d for d in re.findall(r"\{([^}]*)\}", gp.group(1))]
    envs = list(_FIG_ENV_RE.finditer(body))
    numbers = {lab: n for n, m in enumerate(envs, 1) for lab in _LABEL_RE.findall(m.group(2))}

    def with_numbers(t: str) -> str:
        return re.sub(r"\\(?:ref|cref|Cref|autoref)\{([^}]+)\}",
                      lambda mm: str(numbers.get(mm.group(1), "?")), t)

    figures = []
    for n, m in enumerate(envs, 1):
        env = m.group(2)
        cap = ""
        for cm in _CAPTION_RE.finditer(env):
            arg = balanced_braces(env, cm.end() - 1)
            if arg:
                cap += (" " if cap else "") + _clean_tex(arg)
        labels = _LABEL_RE.findall(env)
        fig = Figure(number=n, caption=cap, label=labels[0] if labels else None)
        for g in _INCLUDE_RE.findall(env):
            fig.files.append(g)
            path = _resolve_graphic(g, roots)
            if path:
                fig.resolved.append(str(path))
            else:
                fig.missing.append(g)
        for lab in labels:
            for rm in re.finditer(r"\\(?:ref|cref|Cref|autoref|subref)\{" + re.escape(lab) + r"\}", body):
                if not (m.start() <= rm.start() <= m.end()):
                    fig.mentions.append(_clean_tex(with_numbers(_sentences_around(body, rm.start()))))
        figures.append(fig)
    return figures


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
_V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CAPTION_TEXT_RE = re.compile(r"^\s*(?:Figure|Fig\.?)\s*(\d+)\s*[.:|\u2014\u2013-]?\s*(.*)$", re.I | re.S)


def figures_from_docx(path: Path, cache_dir: Path | None) -> list[Figure]:
    with zipfile.ZipFile(path) as z:
        doc = ElementTree.fromstring(z.read("word/document.xml"))
        rels = {}
        if "word/_rels/document.xml.rels" in z.namelist():
            for r in ElementTree.fromstring(z.read("word/_rels/document.xml.rels")):
                rels[r.get("Id")] = r.get("Target")
        paragraphs = []
        for p in doc.iter(f"{_W}p"):
            text = "".join(t.text or "" for t in p.iter(f"{_W}t"))
            style_el = p.find(f"{_W}pPr/{_W}pStyle")
            style = style_el.get(f"{_W}val") if style_el is not None else ""
            ids = [b.get(_R_EMBED) for b in p.iter(_A_BLIP) if b.get(_R_EMBED)]
            ids += [v.get(_R_ID) for v in p.iter(_V_IMAGEDATA) if v.get(_R_ID)]
            paragraphs.append({"text": text.strip(), "style": style or "", "images": ids})

        def is_caption(par):
            return bool(par["text"]) and ("caption" in par["style"].lower() or _CAPTION_TEXT_RE.match(par["text"]))

        figures: list[Figure] = []
        used_captions: set[int] = set()
        for i, par in enumerate(paragraphs):
            if not par["images"]:
                continue
            cap_idx = next((j for j in list(range(i + 1, min(i + 4, len(paragraphs)))) + [i - 1]
                            if 0 <= j < len(paragraphs) and j not in used_captions and is_caption(paragraphs[j])), None)
            if figures and not figures[-1].caption and cap_idx is None:
                fig = figures[-1]       # another panel of the previous, still-uncaptioned figure
            else:
                fig = Figure(number=len(figures) + 1)
                figures.append(fig)
            if cap_idx is not None:
                used_captions.add(cap_idx)
                fig.caption = paragraphs[cap_idx]["text"]
                cm = _CAPTION_TEXT_RE.match(fig.caption)
                if cm:
                    fig.number = int(cm.group(1))
            for rid in par["images"]:
                target = rels.get(rid)
                if not target:
                    continue
                member = "word/" + target.lstrip("/") if not target.startswith("word/") else target
                fig.files.append(Path(target).name)
                if cache_dir is not None and member in z.namelist():
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    out = cache_dir / f"docx-fig{fig.number}-{Path(target).name}"
                    out.write_bytes(z.read(member))
                    fig.resolved.append(str(out))
        # legends without embedded images (figures uploaded as separate files)
        have = {f.number for f in figures}
        for j, par in enumerate(paragraphs):
            if j in used_captions:
                continue
            cm = _CAPTION_TEXT_RE.match(par["text"])
            if cm and len(par["text"]) > 25 and int(cm.group(1)) not in have and \
                    (par["style"].lower().startswith("caption") or len(cm.group(2).split()) >= 4):
                have.add(int(cm.group(1)))
                figures.append(Figure(number=int(cm.group(1)), caption=par["text"]))
    figures.sort(key=lambda f: f.number)
    return figures


def figures_from_markdown(path: Path) -> list[Figure]:
    text = read_text_file(path)[0]
    figures = []
    for n, m in enumerate(re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)", text), 1):
        fig = Figure(number=n, caption=m.group(1).strip(), files=[m.group(2)])
        p = (path.parent / m.group(2))
        (fig.resolved if p.is_file() else fig.missing).append(str(p) if p.is_file() else m.group(2))
        after = text[m.end():m.end() + 600].strip()
        cm = _CAPTION_TEXT_RE.match(after.split("\n\n")[0]) if after else None
        if cm:
            fig.caption = (fig.caption + " " + after.split("\n\n")[0]).strip()
        figures.append(fig)
    return figures


_CHUNK_RE = re.compile(r"^```\{r\s*([^,}\s]*)([^}]*)\}\s*\n(.*?)^```", re.M | re.S)
_CHUNK_CAP_RE = re.compile(r"""fig\.cap\s*=\s*(["'])(.*?)\1""", re.S)
_QUARTO_OPT_RE = re.compile(r"^#\|\s*([\w.-]+)\s*:\s*(.*)$", re.M)


def figures_from_rmarkdown(path: Path) -> list[Figure]:
    """R Markdown / Quarto: each chunk with a figure caption is a figure, and
    the chunk itself is the code that draws it. Rendered images are matched
    by chunk label in knitr's output folders (<label>-1.png etc.)."""
    text = read_text_file(path)[0]
    rendered = {}
    for p in path.parent.rglob("*"):
        if p.suffix.lower() in IMAGE_EXTS and p.is_file() and len(p.relative_to(path.parent).parts) <= 4:
            rendered.setdefault(re.sub(r"-\d+$", "", p.stem).lower(), p)
    figures = []
    for m in _CHUNK_RE.finditer(text):
        label, opts, body = m.group(1).strip(), m.group(2), m.group(3)
        qopts = dict((k.strip(), v.strip().strip("\"'")) for k, v in _QUARTO_OPT_RE.findall(body))
        label = label or qopts.get("label", "")
        cm = _CHUNK_CAP_RE.search(opts)
        caption = cm.group(2) if cm else qopts.get("fig-cap", "")
        if not caption:
            continue
        line = text.count("\n", 0, m.start()) + 1
        code_lines = body.rstrip("\n").splitlines()
        width = len(str(line + len(code_lines)))
        segment = "\n".join(f"{line + 1 + i:>{width}}| {ln}" for i, ln in enumerate(code_lines))
        fig = Figure(number=len(figures) + 1, caption=caption, label=label or None)
        fig.code.append({"file": path.name, "line": line, "call": f"```{{r {label}{opts}}}", "target": label,
                         "match": "R Markdown chunk", "reads": sorted({Path(r.group(1)).name for r in _READ_RE.finditer(body)}),
                         "segment": segment})
        img = rendered.get(label.lower()) if label else None
        if img:
            fig.files.append(img.name)
            fig.resolved.append(str(img))
        else:
            fig.image_note = "not rendered yet (knit the document to check the image)"
        figures.append(fig)
    return figures


def _mentions_by_number(figures: list[Figure], text: str) -> None:
    for fig in figures:
        if fig.mentions:
            continue
        for m in re.finditer(rf"\bFig(?:ure)?s?\.?\s*{fig.number}(?![0-9])", text):
            sent = _sentences_around(text, m.start())
            if not _CAPTION_TEXT_RE.match(sent) and sent not in fig.mentions:
                fig.mentions.append(sent)


def _separate_figure_files(figures_dir: Path | None) -> dict[int, Path]:
    out: dict[int, Path] = {}
    if not figures_dir or not figures_dir.is_dir():
        return out
    for p in sorted(figures_dir.rglob("*")):
        m = re.search(r"fig(?:ure)?[\s_-]*0*(\d+)", p.stem, re.I)
        if m and p.suffix.lower() in IMAGE_EXTS and int(m.group(1)) not in out:
            out[int(m.group(1))] = p
    return out


# ---------------------------------------------------------------------------
# Which code draws which figure
# ---------------------------------------------------------------------------

_OUTPUT_CALL_RE = re.compile(
    r"\b(ggsave|ggexport|save_plot|pdf|png|jpeg|jpg|tiff|bmp|svg|svglite|cairo_pdf|cairo_ps|postscript|agg_png|"
    r"agg_tiff|dev\.copy|dev\.print|savefig|write_image|saveas|exportgraphics|export_fig|print|imwrite|"
    r"savePlot|plotly::save_image|tikz)\s*\(", re.I)
_SECTION_RE = re.compile(r"^\s*(?:#|%|//)+\s*(?:-{3,}|={3,}|\*{3,}|#{2,}|fig(?:ure)?\.?\s*\d)", re.I)
_DEVICE_OPEN_RE = re.compile(r"\b(pdf|png|jpeg|jpg|tiff|bmp|svg|svglite|cairo_pdf|cairo_ps|postscript|agg_png|"
                             r"agg_tiff)\s*\(", re.I)
_PATH_LITERAL_RE = re.compile(r"""["']([^"'\n]*?\.(?:pdf|png|jpe?g|tiff?|eps|svg|fig|emf|bmp))["']""", re.I)
_FIG_COMMENT_RE = re.compile(r"^\s*(?:#|%|//).*?\bfig(?:ure)?\.?\s*(\d+)", re.I)
_READ_RE = re.compile(r"""(?:read[._](?:csv|table|delim|xlsx|excel|rds|mat|tsv)|readRDS|readMat|read_mat|load|
                         readmatrix|readtable|xlsread|importdata|matfile|loadmat|np\.load|pd\.read_\w+|fread|
                         open)\s*\(\s*(?:file\s*=\s*)?["']([^"']+)["']""", re.I | re.X)


def _call_text(lines: list[str], i: int) -> str:
    """The (possibly multi-line) call starting on line i."""
    text = lines[i]
    depth = text.count("(") - text.count(")")
    j = i
    while depth > 0 and j + 1 < len(lines) and j - i < 8:
        j += 1
        text += " " + lines[j].strip()
        depth += lines[j].count("(") - lines[j].count(")")
    return text


def figure_outputs(code_texts: dict[str, str]) -> list[dict]:
    """Every place a script writes a figure file: {file, line, call, target, stem, index}."""
    outs = []
    for rel, text in code_texts.items():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(("#", "%", "//")):
                continue
            if not _OUTPUT_CALL_RE.search(line):
                continue
            call = _call_text(lines, i)
            for m in _PATH_LITERAL_RE.finditer(call):
                target = m.group(1).replace("\\", "/")
                outs.append({"file": rel, "line": i + 1, "call": " ".join(call.split())[:160], "target": target,
                             "stem": Path(target).stem.lower(), "index": i})
    return outs


def _segment(lines: list[str], save_idx: int, prev_save_idx: int | None) -> str:
    """Plotting code for one figure: from the previous figure's save call (or a
    section comment) up to this save call (and a following dev.off())."""
    start = (prev_save_idx + 1) if prev_save_idx is not None else 0
    for k in range(save_idx, start - 1, -1):
        if _SECTION_RE.match(lines[k]) and k < save_idx:
            start = k
            break
    start = max(start, save_idx - 120)
    call_end = save_idx
    depth = lines[save_idx].count("(") - lines[save_idx].count(")")
    while depth > 0 and call_end + 1 < len(lines):
        call_end += 1
        depth += lines[call_end].count("(") - lines[call_end].count(")")
    end = call_end
    if _DEVICE_OPEN_RE.search(lines[save_idx]):
        # R graphics device: the plot is drawn *after* png()/pdf(), up to dev.off()
        for k in range(call_end + 1, min(len(lines), call_end + 80)):
            if _OUTPUT_CALL_RE.search(lines[k]) and not re.search(r"dev\.off", lines[k]):
                break
            end = k
            if re.search(r"dev\.off\s*\(", lines[k]):
                break
    width = len(str(end + 1))
    body = "\n".join(f"{k + 1:>{width}}| {lines[k]}" for k in range(start, end + 1))
    if start > 0:
        # the script's setup (data loading, derived variables) matters for what is
        # plotted -- but only what comes before the first figure's own code
        first = next((k for k, ln in enumerate(lines)
                      if _SECTION_RE.match(ln) or _OUTPUT_CALL_RE.search(ln)),
                     start)
        setup_end = min(start, first, 40)
        setup = "\n".join(f"{k + 1:>{width}}| {lines[k]}" for k in range(0, setup_end) if lines[k].strip())
        body = f"{setup}\n{'...' if setup_end < start else ''}\n{body}".replace("\n\n", "\n")
    return body


def map_code_to_figures(figures: list[Figure], code_texts: dict[str, str]) -> list[dict]:
    """Attach generating code to each figure; returns outputs no figure uses."""
    outs = figure_outputs(code_texts)
    by_file: dict[str, list[dict]] = {}
    for o in outs:
        by_file.setdefault(o["file"], []).append(o)
    reads: dict[str, list[str]] = {}
    for rel, text in code_texts.items():
        reads[rel] = sorted({Path(m.group(1)).name for m in _READ_RE.finditer(text)})
    used: set[int] = set()
    for fig in figures:
        stems = {Path(f).stem.lower() for f in fig.files} | {Path(f).stem.lower() for f in fig.resolved}
        stems = {re.sub(r"^docx-fig\d+-", "", s) for s in stems}
        for o in outs:
            if o["stem"] in stems:
                used.add(id(o))
                fig.code.append(_code_entry(o, code_texts, by_file, reads, "writes this figure's file"))
        if not fig.code:
            for rel, text in code_texts.items():
                for i, line in enumerate(text.splitlines()):
                    m = _FIG_COMMENT_RE.match(line)
                    if m and int(m.group(1)) == fig.number:
                        nxt = [o for o in by_file.get(rel, []) if o["index"] >= i]
                        if nxt:
                            used.add(id(nxt[0]))
                            fig.code.append(_code_entry(nxt[0], code_texts, by_file, reads,
                                                        f"comment says 'Figure {fig.number}'"))
                        break
    return [o for o in outs if id(o) not in used]


def _code_entry(o: dict, code_texts: dict, by_file: dict, reads: dict, match: str) -> dict:
    lines = code_texts[o["file"]].splitlines()
    prev = [x["index"] for x in by_file[o["file"]] if x["index"] < o["index"]]
    return {"file": o["file"], "line": o["line"], "call": o["call"], "target": o["target"], "match": match,
            "reads": reads.get(o["file"], []), "segment": _segment(lines, o["index"], prev[-1] if prev else None)}


# ---------------------------------------------------------------------------
# Images for vision models
# ---------------------------------------------------------------------------

def image_size(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", head[16:24])
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
    return None


def prepare_image(src: Path, cache_dir: Path, name: str) -> tuple[str | None, str | None]:
    """(path of a PNG/JPG a model can read, note). PDFs are rendered with
    pdftoppm; large rasters are downscaled if Pillow is available."""
    ext = src.suffix.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if src.stat().st_size <= _MAX_MODEL_IMAGE:
            return str(src), None
        return _pil_convert(src, cache_dir / f"{name}.png")
    if ext in (".pdf", ".eps", ".ps"):
        if ext == ".pdf" and PDFTOPPM:
            prefix = cache_dir / name
            try:
                subprocess.run([PDFTOPPM, "-png", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "1600",
                                str(src), str(prefix)], capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if prefix.with_suffix(".png").is_file():
                return str(prefix.with_suffix(".png")), None
        if GHOSTSCRIPT:
            out = cache_dir / f"{name}.png"
            try:
                subprocess.run([GHOSTSCRIPT, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m", "-r150",
                                "-dEPSCrop", f"-sOutputFile={out}", str(src)], capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if out.is_file():
                return str(out), None
        return None, f"could not render {ext} (needs pdftoppm{' or Ghostscript' if ext != '.pdf' else ''})"
    if ext in (".tif", ".tiff", ".bmp"):
        return _pil_convert(src, cache_dir / f"{name}.png")
    return None, f"{ext} images can't be shown to the model"


def _pil_convert(src: Path, out: Path) -> tuple[str | None, str | None]:
    try:
        from PIL import Image
    except ImportError:
        return None, f"{src.suffix} image needs Pillow (pip install pillow) to be shown to the model"
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((1600, 1600))
            im.save(out, "PNG")
        return str(out), None
    except Exception as e:
        return None, f"could not convert image: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def collect_figures(manuscript: Path, code_texts: dict[str, str] | None = None,
                    figures_dir: Path | None = None, cache_dir: Path | None = None,
                    manuscript_text: str | None = None) -> tuple[list[Figure], list[dict]]:
    """Returns (figures, figure outputs written by code that no figure uses)."""
    ext = manuscript.suffix.lower()
    try:
        if ext == ".tex":
            figures = figures_from_tex(manuscript)
        elif ext == ".docx":
            figures = figures_from_docx(manuscript, cache_dir / "docx" if cache_dir else None)
        elif ext in (".rmd", ".qmd"):
            figures = figures_from_rmarkdown(manuscript) or figures_from_markdown(manuscript)
        elif ext in (".md", ".markdown"):
            figures = figures_from_markdown(manuscript)
        else:
            figures = []
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError):
        figures = []
    if manuscript_text:
        _mentions_by_number(figures, manuscript_text)
    separate = _separate_figure_files(figures_dir)
    for fig in figures:
        if not fig.resolved and fig.number in separate:
            fig.resolved.append(str(separate[fig.number]))
            fig.files.append(separate[fig.number].name)
            fig.missing = [m for m in fig.missing if Path(m).stem.lower() != separate[fig.number].stem.lower()]
    unused = map_code_to_figures([f for f in figures if not f.code], code_texts or {})
    for fig in figures:
        if fig.resolved:
            src = Path(fig.resolved[0])
            fig.pixel_size = image_size(src)
            if cache_dir is not None:
                fig.image, fig.image_note = prepare_image(src, cache_dir, f"figure{fig.number}")
            elif src.suffix.lower() in (".png", ".jpg", ".jpeg"):
                fig.image = str(src)
        elif fig.missing:
            fig.image_note = "image file not found"
    return figures, unused


def read_code_texts(paths: list[str]) -> dict[str, str]:
    """rel path -> text for every code file under the given paths."""
    from .code_and_data import collect, read_code
    entries, _notes = collect(paths, "code")
    out = {}
    for e in entries:
        if e.kind == "code" and e.size <= 5_000_000:
            try:
                out[e.rel] = read_code(e)
            except OSError:
                pass
    return out


__all__ = ["Figure", "collect_figures", "figure_outputs", "map_code_to_figures", "prepare_image",
           "read_code_texts", "image_size"]
