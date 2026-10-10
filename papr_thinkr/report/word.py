"""Markdown -> Word (.docx): pandoc when it's installed, otherwise a small
built-in writer (stdlib zipfile), so a .docx is always produced."""
from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from ..inputs.documents import PANDOC


def markdown_to_docx(markdown: str, out_path: str | Path, title: str | None = None) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if PANDOC:
        md_tmp = out.with_suffix(".tmp.md")
        md_tmp.write_text(markdown, encoding="utf-8")
        try:
            args = [PANDOC, str(md_tmp), "-f", "markdown-yaml_metadata_block", "-t", "docx", "-o", str(out)]
            if title:
                args += ["--metadata", f"title={title}"]
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and out.exists():
                return out
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                md_tmp.unlink()
            except OSError:
                pass
    _simple_docx(markdown, out)
    return out


_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def _runs(text: str) -> str:
    out = []
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        props = ""
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, props = part[2:-2], "<w:b/>"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, props = part[1:-1], "<w:i/>"
        elif part.startswith("`") and part.endswith("`"):
            part, props = part[1:-1], '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>'
        part = part.replace("\\|", "|")
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _para(text: str, style: str | None = None, indent: int = 0) -> str:
    ppr = ""
    if style or indent:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + \
              (f'<w:ind w:left="{indent}"/>' if indent else "") + "</w:pPr>"
    return f"<w:p>{ppr}{_runs(text)}</w:p>"


def _simple_docx(markdown: str, out: Path) -> None:
    """Minimal Markdown -> .docx: headings, paragraphs, bullets, block quotes,
    pipe tables, bold/italic/code. Used only when pandoc is unavailable."""
    body: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            body.append(_para(m.group(2), f"Heading{min(len(m.group(1)), 4)}"))
            i += 1
            continue
        if ln.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            tbl = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
                   '<w:tblBorders><w:top w:val="single" w:sz="4"/><w:bottom w:val="single" w:sz="4"/>'
                   '<w:insideH w:val="single" w:sz="4"/><w:insideV w:val="single" w:sz="4"/>'
                   '<w:left w:val="single" w:sz="4"/><w:right w:val="single" w:sz="4"/></w:tblBorders></w:tblPr>']
            for r in rows:
                tbl.append("<w:tr>" + "".join(f"<w:tc><w:tcPr/>{_para(c)}</w:tc>" for c in r) + "</w:tr>")
            tbl.append("</w:tbl>")
            body.append("".join(tbl))
            body.append(_para(""))
            continue
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i][1:].strip())
                i += 1
            for b in " \n".join(buf).split("\n"):
                body.append(_para(b, "Quote", indent=567))
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            depth = len(m.group(1)) // 2
            marker = "• " if m.group(2) in "-*" else m.group(2) + " "
            body.append(_para(marker + m.group(3), indent=360 + 360 * depth))
            i += 1
            continue
        buf = [ln.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|>|\||\s*[-*]\s|\s*\d+\.\s)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        body.append(_para(" ".join(buf).rstrip("  ")))

    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
              '<w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120"/>'
              '</w:pPr></w:pPrDefault></w:docDefaults>'
              + "".join(f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
                        f'<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240"/>'
                        f'<w:outlineLvl w:val="{n - 1}"/></w:pPr><w:rPr><w:b/><w:sz w:val="{sz}"/></w:rPr></w:style>'
                        for n, sz in ((1, 36), (2, 30), (3, 26), (4, 23)))
              + '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>'
              '<w:rPr><w:i/><w:color w:val="555555"/></w:rPr></w:style>'
              '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style>'
              '</w:styles>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(body) +
                '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1200" w:right="1200" '
                'w:bottom="1200" w:left="1200" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
                '</w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                     'officedocument.wordprocessingml.document.main+xml"/>'
                     '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
                     'officedocument.wordprocessingml.styles+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'styles" Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles)
