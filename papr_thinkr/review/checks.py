"""Deterministic manuscript checks. They're cheap, never hallucinate, and
catch the mechanical problems LLM reviewers skim past: leftover placeholders,
broken LaTeX references/citations, figure numbering gaps, duplicated words,
p-values without effect sizes, missing standard statements, etc.

Each finding is returned in the same shape as an LLM critique item (see
review.normalize_item) so it flows through the same report and UI."""
from __future__ import annotations

import re
from pathlib import Path

from ..inputs.documents import read_text_file, tex_component_files


def _item(title, category, severity, issue, suggestion, location=None, quote=None,
          effort_level="trivial", hours=(0.25, 1.0), confidence=0.9, rationale=""):
    return {
        "title": title, "category": category, "severity": severity, "location": location,
        "quote": quote, "issue": issue, "rationale": rationale, "suggestion": suggestion,
        "effort_level": effort_level, "hours_low": hours[0], "hours_high": hours[1],
        "needs_new_data": False, "effort_confidence": 0.8, "confidence": confidence,
        "persona": "Automated checks",
    }


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _examples(text: str, matches: list[re.Match], n: int = 4, width: int = 50) -> str:
    out = []
    for m in matches[:n]:
        a, b = max(0, m.start() - width), min(len(text), m.end() + width)
        snippet = " ".join(text[a:b].split())
        out.append(f"line {_line_of(text, m.start())}: \u201c\u2026{snippet}\u2026\u201d")
    return "; ".join(out)


_PLACEHOLDER_RE = re.compile(
    r"\b(TODO|TBD|FIXME|XXX)\b|\?\?+|\[(?:CITATION NEEDED|CITE|REF|XX|ref|cite|citation)\]|"
    r"Error! Reference source not found|lorem ipsum|\bINSERT\b", re.I)
_DUP_WORD_RE = re.compile(r"\b([A-Za-z]{2,})\s+\1\b", re.I)
_DUP_OK = {"that", "had", "is", "can", "do", "very", "so", "bye", "no", "yes", "ha", "la", "et"}
_PZERO_RE = re.compile(r"\b[pP]\s*[=<]\s*0?\.0{3,}(?!\d*[1-9])")
_PVAL_RE = re.compile(r"\b[pP]\s*[=<>\u2264\u2265]\s*0?\.\d+|\b[pP]\s*<\s*\.\d+")
_EFFECT_RE = re.compile(r"confidence interval|effect size|Cohen|Hedges|odds ratio|hazard ratio|eta squared|"
                        r"standardi[sz]ed (mean )?difference|credible interval|risk ratio|relative risk", re.I)
# Abbreviations must be case-sensitive: "OR" is an odds ratio, "or" is not.
_EFFECT_ABBR_RE = re.compile(r"\b\d{2}\s?%\s?CI\b|\bCI\b|\bOR\b|\bHR\b|\bRR\b|\bR\^?2\b|R\u00b2|\u03b7|"
                             r"\bd\s*=\s*-?\d")
_OVERCLAIM_RE = re.compile(r"\b(prove[sdn]?|proof that|clearly (show|demonstrate)s?|undoubtedly|"
                           r"for the first time|unprecedented|groundbreaking|paradigm[- ]shift|"
                           r"definitively|conclusively)\b", re.I)
_FIG_REF_RE = re.compile(r"\b(Fig(?:ure)?s?\.?|Table)\s*(\d{1,2})(?:\s*[-\u2013]\s*(\d{1,2}))?", re.I)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def check_manuscript(text: str, path: str | None = None) -> list[dict]:
    findings: list[dict] = []
    ext = Path(path).suffix.lower() if path else ""
    plain = text
    if ext == ".tex":
        plain = re.sub(r"(?<!\\)%.*", "", text)

    # 1. Leftover placeholders / drafting notes
    ph = list(_PLACEHOLDER_RE.finditer(plain))
    if ph:
        findings.append(_item(
            f"{len(ph)} leftover placeholder(s) / drafting note(s)", "mechanics", "minor",
            f"The manuscript still contains placeholders or drafting notes: {_examples(plain, ph)}.",
            "Resolve or remove every placeholder before submission.",
            location=f"line {_line_of(plain, ph[0].start())}", quote=ph[0].group(0),
            hours=(0.25, 2.0), confidence=0.95,
            rationale="Editors and reviewers read unfinished placeholders as a sign of a rushed submission."))

    # 2. Duplicated words
    dups = [m for m in _DUP_WORD_RE.finditer(plain) if m.group(1).lower() not in _DUP_OK
            and not m.group(1).isupper()]
    if dups:
        findings.append(_item(
            f"Duplicated word(s) ({len(dups)})", "mechanics", "editorial",
            f"Repeated words found: {_examples(plain, dups, n=6, width=30)}.",
            "Delete the duplicated words.", location=f"line {_line_of(plain, dups[0].start())}",
            quote=dups[0].group(0), hours=(0.1, 0.25), confidence=0.85))

    # 3. LaTeX references, labels and citations
    if ext == ".tex":
        findings.extend(_check_latex(plain, path))

    # 4. Figure/table numbering (plain-text formats)
    if ext != ".tex":
        findings.extend(_check_figure_numbers(plain))

    # 5. p = 0.000
    pz = list(_PZERO_RE.finditer(plain))
    if pz:
        findings.append(_item(
            "Impossible p-values reported as p = 0.000", "statistics", "minor",
            f"p-values cannot be zero; found {_examples(plain, pz)}.",
            "Report as p < 0.001 (or the exact value in scientific notation).",
            location=f"line {_line_of(plain, pz[0].start())}", quote=pz[0].group(0),
            hours=(0.1, 0.5), confidence=0.95))

    # 6. p-values without effect sizes / CIs
    pv = list(_PVAL_RE.finditer(plain))
    if len(pv) >= 3 and not _EFFECT_RE.search(plain) and not _EFFECT_ABBR_RE.search(plain):
        findings.append(_item(
            "p-values reported without effect sizes or confidence intervals", "statistics", "major",
            f"The manuscript reports {len(pv)} p-values but no effect sizes or confidence intervals "
            f"anywhere (e.g. {_examples(plain, pv, n=2)}).",
            "Report an effect size with a 95% CI for every key comparison, not just p-values.",
            location="Results", effort_level="moderate", hours=(3, 12), confidence=0.75,
            rationale="Most journals and reporting guidelines now require effect sizes and uncertainty."))

    # 7. Overclaiming language
    oc = list(_OVERCLAIM_RE.finditer(plain))
    if oc:
        findings.append(_item(
            f"Overclaiming language ({len(oc)} instance{'s' if len(oc) != 1 else ''})", "interpretation",
            "minor", f"Strong certainty/novelty claims that reviewers routinely challenge: {_examples(plain, oc)}.",
            "Replace with calibrated wording (e.g. 'suggests', 'is consistent with') unless the claim is "
            "airtight; drop 'for the first time' unless you have checked the literature exhaustively.",
            location=f"line {_line_of(plain, oc[0].start())}", quote=oc[0].group(0),
            hours=(0.25, 1.0), confidence=0.6))

    # 8. Very long sentences
    prose = re.sub(r"\\[a-zA-Z]+(\[[^\]]*\])?(\{[^}]*\})?", " ", plain) if ext == ".tex" else plain
    prose = re.sub(r"\$[^$]*\$", " ", prose)
    sentences = [s for s in _SENT_SPLIT_RE.split(" ".join(prose.split())) if s]
    long = sorted((s for s in sentences if len(s.split()) > 55), key=lambda s: -len(s.split()))
    if len(long) >= 2:
        ex = "; ".join(f"\u201c{s[:110]}\u2026\u201d ({len(s.split())} words)" for s in long[:3])
        findings.append(_item(
            f"{len(long)} very long sentences (> 55 words)", "clarity", "editorial",
            f"Long sentences hurt readability, e.g. {ex}.",
            "Split each into two or three sentences with one idea each.",
            quote=long[0][:200], hours=(0.5, 2.0), confidence=0.7))

    # 9. Missing standard statements
    low = plain.lower()
    human_or_animal = re.search(r"\b(participants?|patients?|subjects?|volunteers?|mice|rats?|animals?|"
                                r"primates?|chicks?|zebrafish|human tissue)\b", low)
    if human_or_animal and not re.search(r"ethic|institutional review board|\birb\b|iacuc|"
                                          r"approved by|informed consent|animal care|declaration of helsinki", low):
        findings.append(_item(
            "No ethics approval / consent statement found", "ethics", "major",
            f"The study involves {human_or_animal.group(0)} but no ethics approval, IRB/IACUC or consent "
            f"statement was found.", "Add the approving body, protocol number and consent procedure.",
            location="Methods", hours=(0.25, 1.0), confidence=0.65,
            rationale="Missing ethics statements are a desk-rejection reason at most journals."))
    if not re.search(r"limitation", low):
        findings.append(_item(
            "No explicit discussion of limitations", "interpretation", "minor",
            "The word 'limitation' does not appear anywhere in the manuscript.",
            "Add a limitations paragraph to the Discussion covering design, sample and generalisability.",
            location="Discussion", effort_level="small", hours=(1, 3), confidence=0.7))
    if not re.search(r"data (are |is )?(available|availability)|code (is )?(available|availability)|"
                     r"github|zenodo|osf\.io|figshare|dryad|upon (reasonable )?request", low):
        findings.append(_item(
            "No data/code availability statement", "reproducibility", "minor",
            "No data or code availability statement or repository link was found.",
            "State where the data and analysis code can be accessed (repository + DOI), or why they cannot be shared.",
            location="End matter", hours=(0.5, 3), effort_level="small", confidence=0.7))
    if not re.search(r"(conflicts?|competing) of interests?|competing interests?|declarations? of interest|disclosures?", low):
        findings.append(_item(
            "No conflict-of-interest statement", "ethics", "editorial",
            "No competing-interests / conflict-of-interest declaration was found.",
            "Add a competing-interests statement (even if none).", location="End matter",
            hours=(0.1, 0.25), confidence=0.55))

    # 10. Abstract length
    m = re.search(r"(?:\\begin\{abstract\}|^#+\s*abstract\s*$|^abstract\s*:?\s*$)(.*?)(?:\\end\{abstract\}|^#+\s|\n\s*\n\s*\n)",
                  plain, re.I | re.S | re.M)
    if m:
        words = len(re.sub(r"\\[a-zA-Z]+", " ", m.group(1)).split())
        if words > 350:
            findings.append(_item(
                f"Abstract is long ({words} words)", "structure", "minor",
                f"The abstract has about {words} words; most journals cap abstracts at 200-300.",
                "Check the journal limit and tighten the abstract to the key question, approach, numbers and conclusion.",
                location="Abstract", effort_level="small", hours=(0.5, 2), confidence=0.6))
    return findings


def _check_latex(text: str, path: str | None) -> list[dict]:
    out = []
    labels = set(re.findall(r"\\label\{([^}]+)\}", text))
    refs = re.findall(r"\\(?:eq|auto|c|C|page|name)?ref\*?\{([^}]+)\}", text)
    ref_keys = [k.strip() for r in refs for k in r.split(",")]
    undefined = sorted({k for k in ref_keys if k and k not in labels})
    if undefined:
        out.append(_item(
            f"{len(undefined)} undefined cross-reference(s)", "mechanics", "minor",
            "These \\ref keys have no matching \\label (they will render as '??'): " + ", ".join(undefined[:15]),
            "Add the missing labels or fix the reference keys.", quote="\\ref{" + undefined[0] + "}",
            hours=(0.25, 1), confidence=0.9))
    fig_labels = {lab for lab in labels if re.match(r"(fig|tab|tbl)", lab, re.I)}
    unreferenced = sorted(lab for lab in fig_labels if lab not in set(ref_keys))
    if unreferenced:
        out.append(_item(
            f"{len(unreferenced)} figure/table(s) never referenced in the text", "figures_tables", "minor",
            "These figures/tables are labelled but never cited in the text: " + ", ".join(unreferenced[:15]),
            "Reference every figure and table in the text, in numerical order, or remove it.",
            hours=(0.25, 1), confidence=0.85))
    cites = re.findall(r"\\(?:[a-zA-Z]*cite[a-zA-Z]*)\*?(?:\[[^\]]*\]){0,2}\{([^}]+)\}", text)
    cite_keys = {k.strip() for c in cites for k in c.split(",") if k.strip()}
    bib_keys = _bib_keys(text, path)
    if bib_keys is not None and cite_keys:
        missing = sorted(cite_keys - bib_keys)
        if missing:
            out.append(_item(
                f"{len(missing)} citation key(s) missing from the bibliography", "mechanics", "minor",
                "These \\cite keys are not in the .bib file(s)/thebibliography: " + ", ".join(missing[:15]),
                "Add the entries or fix the keys (they will render as [?]).", quote=missing[0],
                hours=(0.25, 1.5), confidence=0.9))
    return out


def _bib_keys(text: str, path: str | None) -> set[str] | None:
    keys: set[str] = set(re.findall(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}", text))
    bib_names = []
    for m in re.finditer(r"\\(?:bibliography|addbibresource)\{([^}]+)\}", text):
        bib_names.extend(n.strip() for n in m.group(1).split(","))
    if not bib_names and not keys:
        return None
    if path:
        base = Path(path).resolve().parent
        found_any = bool(keys)
        roots = {base} | {p.parent for p in tex_component_files(path)}
        for name in bib_names:
            fname = name if name.endswith(".bib") else name + ".bib"
            for root in roots:
                bib = root / fname
                if bib.exists():
                    found_any = True
                    keys |= set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", read_text_file(bib)[0]))
                    break
        if not found_any:
            return None
    return keys


def _check_figure_numbers(text: str) -> list[dict]:
    out = []
    for kind_re, kind in ((r"fig", "Figure"), (r"tab", "Table")):
        nums = []
        order = []
        for m in _FIG_REF_RE.finditer(text):
            if not m.group(1).lower().startswith(kind_re):
                continue
            a = int(m.group(2))
            b = int(m.group(3)) if m.group(3) else a
            if 0 < a <= b <= 40:
                nums.extend(range(a, b + 1))
                order.append(a)
        if not nums:
            continue
        present = set(nums)
        missing = [n for n in range(1, max(present) + 1) if n not in present]
        if missing:
            out.append(_item(
                f"{kind} numbering gap: {kind} {', '.join(map(str, missing))} never mentioned",
                "figures_tables", "minor",
                f"The text mentions {kind}s up to {max(present)} but never {kind} "
                f"{', '.join(map(str, missing))}; a {kind.lower()} is missing, unreferenced, or misnumbered.",
                f"Check that every {kind.lower()} exists, is cited in the text, and is numbered consecutively.",
                hours=(0.25, 1), confidence=0.7))
        first_seen = []
        for n in order:
            if n not in first_seen:
                first_seen.append(n)
        if first_seen != sorted(first_seen):
            out.append(_item(
                f"{kind}s are first cited out of order", "figures_tables", "editorial",
                f"{kind}s are first referenced in the order {first_seen[:10]}; journals expect "
                f"first citations in numerical order.", f"Renumber the {kind.lower()}s or reorder the text.",
                hours=(0.25, 1), confidence=0.6))
    return out


# ---------------------------------------------------------------------------
# Figures vs the code that draws them
# ---------------------------------------------------------------------------

_ERRORBAR_CODE = re.compile(r"geom_errorbar|geom_linerange|geom_pointrange|geom_crossbar|stat_summary|"
                            r"\berrorbar\s*\(|arrows\s*\([^)]*angle\s*=\s*90|plt\.errorbar|\.errorbar\(|yerr\s*=|"
                            r"plotCI|errbar|shadedErrorBar|boundedline", re.I)
_SEM_CODE = re.compile(r"sd\s*\([^)]*\)\s*/\s*sqrt|std\s*\([^)]*\)\s*\.?/\s*sqrt|mean_se\b|\bsem\s*\(|"
                       r"std_error|\bse\s*=|stats\.sem|\.sem\(\)|standard\.error|std\.error|/\s*sqrt\s*\(\s*(?:n|length)", re.I)
_SD_CODE = re.compile(r"mean_sdl|\bsd\s*=\s*sd\(|ymin\s*=\s*\w+\s*-\s*sd\b|\bstd\s*\((?![^)]*\)\s*\.?/\s*sqrt)", re.I)
_CI_CODE = re.compile(r"mean_cl_(boot|normal)|qt\s*\(|1\.96|conf\.int|bootci|\bci\s*=", re.I)
_CAPTION_SEM = re.compile(r"\b(SEM|S\.E\.M\.|SE|s\.e\.|standard errors?( of the mean)?)\b", re.I)
_CAPTION_SD = re.compile(r"\b(SD|S\.D\.|standard deviations?)\b", re.I)
_CAPTION_CI = re.compile(r"\b(CI|confidence intervals?|credible intervals?)\b", re.I)
_CAPTION_N = re.compile(r"\bn\s*=\s*\d+", re.I)
_DROPS_ROWS = re.compile(r"!\s*is\.na|na\.omit|complete\.cases|drop_?na|\.dropna|rmmissing|~\s*isnan|"
                         r"na\.rm\s*=\s*T", re.I)
_RED = re.compile(r"[\"'](red|#?f{2}0{4}|#?e41a1c|darkred|firebrick)[\"']|'r'|\"r\"", re.I)
_GREEN = re.compile(r"[\"'](green|#?0{2}f{2}0{2}|#?4daf4a|darkgreen|forestgreen|lime)[\"']|'g'|\"g\"", re.I)
_BAR_OF_MEANS = re.compile(r"geom_col|geom_bar\s*\([^)]*stat\s*=\s*['\"]identity|barplot\s*\(|\bbar\s*\(|plt\.bar\s*\(|"
                           r"\.bar\s*\(", re.I)
_SHOWS_POINTS = re.compile(r"geom_point|geom_jitter|geom_beeswarm|geom_quasirandom|geom_dotplot|stripchart|"
                           r"beeswarm|swarm|scatter|plotSpread|points\s*\(", re.I)


def _fig_item(fig, title, category, severity, issue, suggestion, quote=None, hours=(0.25, 1.0), confidence=0.7,
              effort_level="trivial", where=None):
    it = _item(title, category, severity, issue, suggestion, location=where or f"Figure {fig.number}",
               quote=quote, effort_level=effort_level, hours=hours, confidence=confidence)
    it["persona"] = "Figure & code checks"
    return it


def check_figures(figures, code_provided: bool) -> list[dict]:
    """Deterministic checks of each figure against its caption and the code
    that draws it (see inputs/figures.py)."""
    out = []
    for fig in figures:
        cap = fig.caption or ""
        code = "\n".join(c.get("segment", "") for c in fig.code)
        code_lines = "\n".join(ln for ln in code.splitlines() if not re.match(r"^\s*\d+\|\s*(#|%|//)", ln))
        where = f"{fig.code[0]['file']}:{fig.code[0]['line']}" if fig.code else None
        if fig.missing:
            out.append(_fig_item(fig, f"Figure {fig.number}: image file not found", "figures_tables", "minor",
                                 f"The manuscript includes {', '.join(fig.missing)}, which does not exist, so the "
                                 f"figure cannot be checked (and the document won't compile with it).",
                                 "Add the file (re-run its script) or fix the path.", confidence=0.95))
        if len(cap.split()) < 8:
            out.append(_fig_item(fig, f"Figure {fig.number}: caption too short", "figures_tables", "minor",
                                 f"The caption (\u201c{cap or 'none'}\u201d) doesn't say what is plotted, the sample size, "
                                 f"or what bars/whiskers show; a figure should be understandable without the text.",
                                 "Expand the caption: what each panel shows, n per group, what error bars/boxes "
                                 "represent, and the statistical test.", quote=cap or None, confidence=0.8,
                                 hours=(0.25, 1)))
        if code_provided and not fig.code and (fig.resolved or fig.missing):
            out.append(_fig_item(fig, f"Figure {fig.number}: no script found that makes it", "reproducibility",
                                 "minor", "None of the code provided writes this figure's file "
                                 f"({', '.join(fig.files) or 'unknown'}), so it can't be regenerated or checked.",
                                 "Include the script that produces it (and save to the same file name the manuscript uses).",
                                 confidence=0.5, hours=(0.5, 2)))
        if not code:
            continue
        has_bars = bool(_ERRORBAR_CODE.search(code_lines))
        code_sem, code_sd, code_ci = (bool(_SEM_CODE.search(code_lines)), bool(_SD_CODE.search(code_lines)),
                                      bool(_CI_CODE.search(code_lines)))
        cap_sem, cap_sd, cap_ci = bool(_CAPTION_SEM.search(cap)), bool(_CAPTION_SD.search(cap)), bool(_CAPTION_CI.search(cap))
        if has_bars and not (cap_sem or cap_sd or cap_ci):
            out.append(_fig_item(fig, f"Figure {fig.number}: error bars not defined", "figures_tables", "minor",
                                 "The code draws error bars but the caption never says whether they are SD, SEM or "
                                 "a confidence interval.", "State in the caption what the error bars show.",
                                 confidence=0.85, where=where))
        mismatch = None
        if cap_sd and code_sem and not code_sd:
            mismatch = ("SD", "the standard error (SD/\u221an)")
        elif cap_sem and code_sd and not code_sem:
            mismatch = ("SEM", "the standard deviation")
        elif (cap_sd or cap_sem) and code_ci and not (code_sd or code_sem):
            mismatch = ("SD" if cap_sd else "SEM", "a confidence interval")
        if mismatch:
            m = _SEM_CODE.search(code) if mismatch[1].startswith("the standard error") else (
                _SD_CODE.search(code) or _CI_CODE.search(code))
            line = next((ln for ln in code.splitlines() if m and m.group(0) in ln), "")
            out.append(_fig_item(fig, f"Figure {fig.number}: caption says {mismatch[0]} but the code plots "
                                      f"{mismatch[1].split(' (')[0]}", "figures_tables", "major",
                                 f"The caption says the error bars are {mismatch[0]}, but the plotting code computes "
                                 f"{mismatch[1]}: `{line.strip()[:160]}`. One of them is wrong, and SEM bars look "
                                 f"~\u221an times tighter than SD bars.",
                                 "Decide which you mean, fix the code or the caption, and check the same quantity is "
                                 "used in the text.", quote=line.split("|", 1)[-1].strip()[:200] or None,
                                 confidence=0.75, hours=(0.25, 1.5), effort_level="small", where=where))
        if _CAPTION_N.search(cap) and _DROPS_ROWS.search(code_lines):
            drop = next((ln for ln in code.splitlines() if _DROPS_ROWS.search(ln) and not
                         re.match(r"^\s*\d+\|\s*(#|%)", ln)), "")
            out.append(_fig_item(fig, f"Figure {fig.number}: check the n in the caption", "data", "minor",
                                 f"The caption states {_CAPTION_N.search(cap).group(0)}, but the plotting code "
                                 f"filters the data (`{drop.strip()[:140]}`), so the plotted n may be smaller.",
                                 "Count the rows actually plotted per group and report that n (and the exclusions).",
                                 confidence=0.55, hours=(0.25, 1), where=where))
        if _RED.search(code_lines) and _GREEN.search(code_lines):
            out.append(_fig_item(fig, f"Figure {fig.number}: red/green colours", "figures_tables", "editorial",
                                 "The figure uses red and green to distinguish groups, which ~8% of men cannot tell apart.",
                                 "Use a colour-blind-safe palette (e.g. viridis, Okabe-Ito) and/or different shapes.",
                                 confidence=0.75, where=where))
        if _BAR_OF_MEANS.search(code_lines) and not _SHOWS_POINTS.search(code_lines):
            out.append(_fig_item(fig, f"Figure {fig.number}: bar chart hides the data", "figures_tables", "minor",
                                 "Means are shown as bars without the individual data points; with small n, journals "
                                 "and reviewers increasingly ask to see every data point.",
                                 "Overlay the individual points (geom_jitter/beeswarm) or use a dot/box plot.",
                                 confidence=0.6, hours=(0.5, 2), effort_level="small", where=where))
        if fig.pixel_size and fig.pixel_size[0] < 1200 and Path(fig.resolved[0]).suffix.lower() in (".png", ".jpg", ".jpeg"):
            w, h = fig.pixel_size
            out.append(_fig_item(fig, f"Figure {fig.number}: low-resolution image ({w}\u00d7{h} px)", "figures_tables",
                                 "editorial", f"The figure is a {w}\u00d7{h} px raster; at a typical 8.5 cm column width "
                                 f"that is about {w / 3.35:.0f} dpi, below the 300 dpi most journals require.",
                                 "Export as vector PDF/EPS, or raster at \u2265300 dpi at print size.",
                                 confidence=0.6, where=where))
    return out

