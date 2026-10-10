"""The review report: everything in the Word document -- verdict, effort and
confidence, reviewer-comment triage with drafted rebuttals, conflicts between
reviewers, the independent critique, and the code/data audit."""
from __future__ import annotations

import datetime as _dt
import re
import time
from pathlib import Path

from ..review.effort import aggregate_effort, describe_hours
from .word import markdown_to_docx


REC_LABEL = {"accept": "Accept", "minor_revision": "Minor revision", "major_revision": "Major revision",
             "reject": "Reject"}


STANCE_LABEL = {"address": "Address", "address_partially": "Address partially", "clarify": "Clarify",
                "rebut": "Rebut"}


MISREAD_LABEL = {"missed_existing_text": "asks for something the manuscript already contains",
                 "misread": "misread what the manuscript says", "terminology": "uses a term differently",
                 "wrong_premise": "rests on an incorrect premise",
                 "different_field_norms": "applies conventions from another field"}
AGREE_LABEL = {"agree": "Reviewer is right", "partially_agree": "Reviewer is partly right",
               "disagree": "Reviewer is mistaken"}


def _pct(x) -> str:
    try:
        return f"{float(x):.0%}"
    except (TypeError, ValueError):
        return "n/a"


def _conf_word(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    return "high" if x >= 0.75 else "moderate" if x >= 0.5 else "low"


def _md_escape_cell(s: str) -> str:
    return " ".join(str(s or "").split()).replace("|", "\\|")


def _quote(text: str) -> str:
    return "\n".join("> " + ln if ln.strip() else ">" for ln in (text or "").strip().splitlines())


def _effort_line(e: dict) -> str:
    if not e:
        return "n/a"
    extra = " · needs new data" if e.get("needs_new_data") else ""
    return (f"{e['level'].replace('_', ' ')} · {describe_hours(e['hours_low'], e['hours_high'])} · "
            f"confidence {_pct(e.get('confidence'))}{extra}")


def build_report_markdown(project) -> str:
    cfg = project.config
    review = project.state.get("review") or {}
    comments = project.state.get("comments", [])
    reviewer_comments = [c for c in comments if c.get("origin", "reviewer") == "reviewer"]
    ai_items = [c for c in comments if c.get("origin") == "ai-review" and not c.get("stale")]
    overall = review.get("overall") or {}
    title = cfg.get("title") or cfg.get("name")
    when = _dt.datetime.fromtimestamp(review.get("generated_at") or time.time()).strftime("%Y-%m-%d %H:%M")
    mode = review.get("mode") or ("revision" if reviewer_comments else "first-draft")

    L: list[str] = []
    L.append(f"# Manuscript review: {title}")
    L.append("")
    meta_bits = [f"Generated {when} by papr-thinkr"]
    if review.get("backend"):
        meta_bits.append(f"model: {review['backend']}")
    meta_bits.append("mode: " + ("revision assessment" if mode == "revision" else "first-draft review"))
    if review.get("depth"):
        meta_bits.append(f"depth: {review['depth']}")
    L.append("*" + " · ".join(meta_bits) + "*")
    L.append("")
    if cfg.get("journal"):
        L.append(f"**Target journal:** {cfg['journal']}  ")
    ms = (review.get("manuscript") or {}).get("path") or (cfg.get("manuscript_files") or [{}])[0].get("path", "")
    L.append(f"**Manuscript file:** `{ms}`")
    L.append("")
    L.append("> This report was written by an AI reviewer instructed to be liberal with criticism. Treat every "
             "item as a hypothesis to check against your manuscript, not a verdict. Confidence values are the "
             "model's own calibrated guesses; quotes marked *unverified* could not be found verbatim in the "
             "manuscript.")
    L.append("")

    if not review:
        L.append("_No full review has been run for this project yet. Run one from the app (▶ Run review) "
                 "or with `python papr.py review`._")
        L.append("")

    # ---------------- At a glance ----------------
    eff = review.get("effort") or {}
    L.append("## At a glance")
    L.append("")
    L.append("| Measure | Value |")
    L.append("|---|---|")
    if overall.get("recommendation"):
        L.append(f"| Recommendation (current version) | **{REC_LABEL.get(overall['recommendation'], overall['recommendation'])}** "
                 f"(confidence {_pct(overall.get('recommendation_confidence'))}) |")
    if overall.get("effort_hours_high"):
        L.append(f"| Estimated revision effort | **{describe_hours(overall['effort_hours_low'], overall['effort_hours_high'])}** "
                 f"({(overall.get('effort_level') or '').replace('_', ' ')}) |")
        L.append(f"| Confidence in effort estimate | **{_pct(overall.get('effort_confidence'))}** "
                 f"({_conf_word(overall.get('effort_confidence'))}) |")
    rc = eff.get("reviewer_comments")
    if rc:
        L.append(f"| Bottom-up: reviewer comments | {describe_hours(rc['hours_low'], rc['hours_high'])}, "
                 f"likely ~{rc['hours_likely']:g} h, confidence {_pct(rc['confidence'])} |")
    im = eff.get("independent_major")
    if im:
        L.append(f"| Bottom-up: critical + major independent findings | {describe_hours(im['hours_low'], im['hours_high'])}, "
                 f"confidence {_pct(im['confidence'])} |")
    ind = eff.get("independent")
    if ind:
        L.append(f"| Bottom-up: all independent findings | {describe_hours(ind['hours_low'], ind['hours_high'])} |")
    if reviewer_comments:
        tri = [c.get("triage") for c in reviewer_comments if c.get("triage")]
        counts = {k: sum(1 for t in tri if t["stance"] == k) for k in STANCE_LABEL}
        L.append(f"| Reviewer comments | {len(reviewer_comments)} "
                 f"({', '.join(f'{STANCE_LABEL[k].lower()} {v}' for k, v in counts.items() if v)}) |")
    if ai_items:
        sev = {s: sum(1 for c in ai_items if c.get("severity") == s) for s in ("critical", "major", "minor", "editorial")}
        L.append(f"| Independent findings | {len(ai_items)} "
                 f"({', '.join(f'{k} {v}' for k, v in sev.items() if v)}) |")
    new_data = sum(1 for c in ai_items if ((c.get("critique") or {}).get("effort") or {}).get("needs_new_data")) + \
        sum(1 for c in reviewer_comments if ((c.get("triage") or {}).get("effort") or {}).get("needs_new_data"))
    if new_data:
        L.append(f"| Items needing new data/experiments | {new_data} |")
    L.append("")
    if overall.get("effort_rationale"):
        L.append(f"**Why this effort estimate:** {overall['effort_rationale']}")
        L.append("")
    if overall.get("likely_outcome"):
        L.append(f"**Likely outcome:** {overall['likely_outcome']}")
        L.append("")

    if overall.get("summary"):
        L.append("## Summary")
        L.append("")
        L.append(overall["summary"])
        L.append("")
    if overall.get("strengths"):
        L.append("## Strengths")
        L.append("")
        L.extend(f"- {s}" for s in overall["strengths"])
        L.append("")
    if overall.get("key_weaknesses"):
        L.append("## Key weaknesses")
        L.append("")
        L.extend(f"{i}. {s}" for i, s in enumerate(overall["key_weaknesses"], 1))
        L.append("")
    if overall.get("top_priorities"):
        L.append("## Top priorities")
        L.append("")
        for i, p in enumerate(overall["top_priorities"], 1):
            ids = f" ({', '.join(p['item_ids'])})" if p.get("item_ids") else ""
            L.append(f"{i}. **{p['title']}**{ids} — {p.get('why', '')}")
        L.append("")

    # ---------------- Quick wins ----------------
    quick = []
    for c in reviewer_comments:
        t = c.get("triage") or {}
        e = t.get("effort") or {}
        if t and e.get("hours_high", 99) <= 2 and t.get("stance") != "rebut":
            quick.append((e["hours_high"], f"{c['id']} ({c['reviewer']}): " +
                          ((c.get("decoded") or {}).get("plain_language") or c["comment_text"])[:160]))
    for c in ai_items:
        cr = c.get("critique") or {}
        e = cr.get("effort") or {}
        if e.get("hours_high", 99) <= 1 and c.get("severity") in ("critical", "major", "minor") \
                and cr.get("confidence", 0) >= 0.6:
            quick.append((e["hours_high"], f"{c['id']}: {cr.get('title')}"))
    if quick:
        quick.sort(key=lambda q: q[0])
        L.append("## Quick wins: do these first")
        L.append("")
        L.append("Small, low-risk fixes (about two hours or less each). Clearing them early shrinks the list and "
                 "shows reviewers good faith.")
        L.append("")
        L.extend(f"- {q[1]} *(\u2264{q[0]:g} h)*" for q in quick[:15])
        L.append("")

    # ---------------- Effort breakdown ----------------
    all_eff = []
    for c in ai_items:
        cr = c.get("critique") or {}
        if cr.get("effort"):
            all_eff.append((c.get("severity"), cr.get("category", "other"), cr["effort"]))
    if all_eff:
        L.append("## Effort breakdown (independent findings)")
        L.append("")
        L.append("| Severity | Items | Hours (low–high) | Mean confidence |")
        L.append("|---|---:|---:|---:|")
        for s in ("critical", "major", "minor", "editorial"):
            es = [e for sv, _c, e in all_eff if sv == s]
            if es:
                a = aggregate_effort(es)
                L.append(f"| {s} | {len(es)} | {a['hours_low']:g}–{a['hours_high']:g} | {_pct(a['confidence'])} |")
        L.append("")
        cats: dict[str, list] = {}
        for _s, cat, e in all_eff:
            cats.setdefault(cat, []).append(e)
        L.append("| Category | Items | Hours (low–high) |")
        L.append("|---|---:|---:|")
        for cat, es in sorted(cats.items(), key=lambda kv: -sum(e["hours_high"] for e in kv[1])):
            a = aggregate_effort(es)
            L.append(f"| {cat.replace('_', ' ')} | {len(es)} | {a['hours_low']:g}–{a['hours_high']:g} |")
        L.append("")

    # ---------------- Reviewer comments ----------------
    if reviewer_comments:
        L.append("## Reviewer comments: assessment and response plan")
        L.append("")
        if overall.get("rebuttal_strategy"):
            L.append(f"**Strategy:** {overall['rebuttal_strategy']}")
            L.append("")
        rebuts = [c for c in reviewer_comments if (c.get("triage") or {}).get("stance") in ("rebut", "clarify")]
        if rebuts:
            L.append("### Comments recommended for rebuttal or clarification")
            L.append("")
            L.append("| Comment | Stance | Reviewer is | Chance editor accepts pushback |")
            L.append("|---|---|---|---:|")
            for c in rebuts:
                t = c["triage"]
                L.append(f"| {c['id']} ({_md_escape_cell(c['reviewer'])}) | {STANCE_LABEL[t['stance']]} | "
                         f"{t['agreement'].replace('_', ' ')} ({_pct(t['agreement_confidence'])}) | "
                         f"{_pct(t.get('rebuttal_confidence'))} |")
            L.append("")
        by_rev: dict[str, list[dict]] = {}
        for c in reviewer_comments:
            by_rev.setdefault(c["reviewer"], []).append(c)
        for rev, items in by_rev.items():
            L.append(f"### {rev}")
            L.append("")
            for c in sorted(items, key=lambda x: x.get("order", 0)):
                text = re.sub(r"^\s*(\(?\d{1,2}[.)]|[-*•]|minor:|major:)\s+", "", " ".join(c["comment_text"].split()),
                              flags=re.I)
                first = re.split(r"(?<=[.?!])\s", text, maxsplit=1)[0][:110]
                L.append(f"#### {c['id']} — {first}")
                L.append("")
                L.append(_quote(c["comment_text"]))
                L.append("")
                for r in c.get("resolved_refs") or []:
                    L.append(f"*The reviewer's \u201c{r['ref']}\u201d (submitted PDF{', p. ' + str(r['page']) if r.get('page') else ''}) "
                             f"reads:* \u201c{r['text']}\u201d")
                    L.append("")
                d = c.get("decoded") or {}
                if d.get("plain_language"):
                    L.append(f"- **In plain words:** {d['plain_language']}")
                    if d.get("underlying_concern"):
                        L.append(f"- **What's behind it:** {d['underlying_concern']}")
                    if d.get("what_would_satisfy"):
                        L.append("- **What would satisfy them:**")
                        L.extend(f"    - {x}" for x in d["what_would_satisfy"])
                    if d.get("clarity") != "clear" and d.get("interpretations"):
                        L.append(f"- **Unclear comment -- possible readings:** " + "; ".join(
                            f"({i}) {x['reading']} [{_pct(x['likelihood'])}]" for i, x in enumerate(d["interpretations"], 1)))
                        if d.get("clarification_request"):
                            L.append(f"- **Say in the response how you read it:** \u201c{d['clarification_request']}\u201d")
                    if d.get("misunderstanding", "none") != "none":
                        flag = "" if d.get("evidence_verified") is not False else " *(quote not found verbatim -- check)*"
                        L.append(f"- **Possible misreading:** the reviewer {MISREAD_LABEL.get(d['misunderstanding'], d['misunderstanding'])}. "
                                 f"{d.get('misunderstanding_explanation', '')}")
                        if d.get("manuscript_evidence"):
                            L.append(f"    - Your manuscript{' (' + d['evidence_location'] + ')' if d.get('evidence_location') else ''}: "
                                     f"\u201c{d['manuscript_evidence']}\u201d{flag}")
                        if d.get("clarifying_edit"):
                            L.append(f"    - Clarifying edit so nobody misreads it again: {d['clarifying_edit']}")
                    if d.get("tone") in ("curt", "hostile") and d.get("neutral_rewrite"):
                        L.append(f"- **Calm version of this comment:** {d['neutral_rewrite']}")
                    L.append("")
                t = c.get("triage")
                if not t:
                    L.append("_Not yet assessed (run the review)._")
                    L.append("")
                else:
                    L.append(f"- **Assessment:** {AGREE_LABEL.get(t['agreement'], t['agreement'])} "
                             f"(confidence {_pct(t['agreement_confidence'])})")
                    L.append(f"- **Recommended response:** {STANCE_LABEL.get(t['stance'], t['stance'])}")
                    L.append(f"- **Effort:** {_effort_line(t['effort'])}")
                    L.append(f"- **Already addressed in current manuscript:** {t.get('already_addressed', 'no')}")
                    if t.get("rationale"):
                        L.append(f"- **Rationale:** {t['rationale']}")
                    if t.get("evidence"):
                        L.append(f"- **Relevant manuscript text:** “{t['evidence']}”")
                    if t.get("required_changes"):
                        L.append("- **Required changes:**")
                        L.extend(f"    - {x}" for x in t["required_changes"])
                    L.append("")
                    if t.get("rebuttal"):
                        L.append(f"**Draft rebuttal** (chance the editor accepts it: {_pct(t.get('rebuttal_confidence'))}):")
                        L.append("")
                        L.append(_quote(t["rebuttal"]))
                        L.append("")
                if c.get("status") and c["status"] != "unaddressed":
                    L.append(f"*Status: {c['status'].replace('_', ' ')}*" +
                             (f" — {c['status_evidence']}" if c.get("status_evidence") else ""))
                    L.append("")
                if c.get("response_text"):
                    L.append("**Response drafted in app:**")
                    L.append("")
                    L.append(_quote(c["response_text"]))
                    L.append("")

    cross = review.get("cross_comment") or {}
    if cross.get("reviewers"):
        L.append("## Who the reviewers are and how to answer them")
        L.append("")
        for r in cross["reviewers"]:
            L.append(f"**{r['reviewer']}** \u2014 likely expertise: {r.get('expertise_guess') or 'unclear'}; "
                     f"tone: {r.get('tone') or 'n/a'}.")
            if r.get("main_concerns"):
                L.append(f"Main concerns: {'; '.join(r['main_concerns'])}.")
            if r.get("how_to_respond"):
                L.append(f"*How to respond:* {r['how_to_respond']}")
            L.append("")
    if cross.get("conflicts") or cross.get("duplicates") or cross.get("themes"):
        L.append("## Across reviewers")
        L.append("")
        if cross.get("conflicts"):
            L.append("### Conflicting requests")
            L.append("")
            for g in cross["conflicts"]:
                L.append(f"- **{', '.join(g['comment_ids'])}:** {g.get('description', '')} "
                         f"*Suggested resolution:* {g.get('suggested_resolution', '')}")
            L.append("")
        if cross.get("duplicates"):
            L.append("### Same point raised by several reviewers")
            L.append("")
            for g in cross["duplicates"]:
                L.append(f"- **{', '.join(g['comment_ids'])}:** {g.get('theme', '')}")
            L.append("")
        if cross.get("themes"):
            L.append("### Main themes")
            L.append("")
            for g in cross["themes"]:
                L.append(f"- {g.get('theme', '')} ({len(g['comment_ids'])} comments)")
            L.append("")

    # ---------------- Independent critique ----------------
    if ai_items:
        heading = ("## Independent critique: what the reviewers did not raise" if reviewer_comments
                   else "## Detailed critique")
        L.append(heading)
        L.append("")
        L.append("Each finding lists severity, category, location, how sure the reviewer is that it is a real "
                 "problem (confidence), and the estimated effort to fix it.")
        L.append("")
        for sev in ("critical", "major", "minor", "editorial"):
            group = [c for c in ai_items if c.get("severity") == sev]
            if not group:
                continue
            L.append(f"### {sev.capitalize()} ({len(group)})")
            L.append("")
            for c in sorted(group, key=lambda c: -((c.get("critique") or {}).get("confidence") or 0)):
                cr = c.get("critique") or {}
                L.append(f"#### {c['id']} · {cr.get('title') or c['comment_text'][:80]}")
                L.append("")
                bits = [cr.get("category", "other").replace("_", " ")]
                if cr.get("location"):
                    bits.append(cr["location"])
                bits.append(f"confidence {_pct(cr.get('confidence'))}")
                if cr.get("support", 1) > 1:
                    bits.append(f"raised by {cr['support']} reviewers")
                L.append(f"*{' · '.join(bits)}*")
                L.append("")
                if cr.get("quote"):
                    flag = "" if cr.get("quote_verified") is not False else " *(unverified quote)*"
                    L.append(f"> “{cr['quote']}”{flag}")
                    L.append("")
                L.append(f"**Issue.** {cr.get('issue') or c['comment_text']}")
                L.append("")
                if cr.get("rationale"):
                    L.append(f"**Why it matters.** {cr['rationale']}")
                    L.append("")
                if cr.get("suggestion"):
                    L.append(f"**Suggested fix.** {cr['suggestion']}")
                    L.append("")
                L.append(f"**Effort.** {_effort_line(cr.get('effort'))}")
                L.append("")
                if c.get("author_notes"):
                    L.append(f"*Author notes:* {c['author_notes']}")
                    L.append("")

    # ---------------- Figures ----------------
    figures = review.get("figures") or []
    if figures:
        L.append("## Figures and the code that draws them")
        L.append("")
        L.append("| Figure | Image | Drawn by | Data read | Findings |")
        L.append("|---|---|---|---|---:|")
        for f in figures:
            n_find = sum(1 for c in ai_items if re.match(rf"^Figure {f['number']}\b", (c.get("location_ref") or "")) or
                         ((c.get("critique") or {}).get("title") or "").startswith(f"Figure {f['number']}:"))
            img = ("missing: " + ", ".join(f["missing"])) if f.get("missing") else (", ".join(Path(r).name for r in f.get("resolved") or []) or "n/a")
            code = "; ".join(f"{c['file']}:{c['line']}" for c in f.get("code") or []) or "not found"
            reads = ", ".join(sorted({r for c in f.get("code") or [] for r in c.get("reads", [])})) or "\u2014"
            L.append(f"| {f['number']} | {_md_escape_cell(img)} | {_md_escape_cell(code)} | {_md_escape_cell(reads)} | {n_find} |")
        L.append("")
        if review.get("unused_figure_outputs"):
            L.append("Figure files written by the code but not used in the manuscript: " +
                     ", ".join(f"{o['target']} ({o['file']}:{o['line']})" for o in review["unused_figure_outputs"][:10]) + ".")
            L.append("")

    # ---------------- Code & data ----------------
    ca = review.get("code_audit")
    if ca:
        L.append("## Code and data provided")
        L.append("")
        L.append(f"{ca['n_files']} file(s): {ca['n_code_files']} code, {ca['n_data_files']} data."
                 + (" Some content was truncated to fit the model's context." if ca.get("truncated") else ""))
        L.append("")
        ds = [s for s in ca.get("data_summaries", []) if "n_rows" in s]
        if ds:
            L.append("| Data file | Rows | Columns | Duplicate rows | Columns with missing values |")
            L.append("|---|---:|---:|---:|---|")
            for s in ds:
                miss = ", ".join(f"{c['name']} ({c['missing_pct']}%)" for c in s.get("columns", []) if c.get("missing"))
                L.append(f"| {_md_escape_cell(s['file'])} | {s['n_rows']}{'+' if s.get('rows_capped') else ''} | "
                         f"{s['n_cols']} | {s.get('duplicate_rows', 0)} | {_md_escape_cell(miss[:200]) or 'none'} |")
            L.append("")
        for n in ca.get("notes") or []:
            L.append(f"- {n}")
        L.append("Findings from the code/data audit are included in the critique above (categories *code*, "
                 "*data*, *reproducibility*).")
        L.append("")

    # ---------------- Method ----------------
    if review:
        L.append("## How this review was produced")
        L.append("")
        u = review.get("usage") or {}
        L.append(f"- Model: {review.get('backend')}; depth: {review.get('depth')}; "
                 f"{u.get('calls', 0)} model calls; {review.get('elapsed_seconds', 0) / 60:.1f} min.")
        if review.get("personas"):
            L.append(f"- Referee perspectives: {', '.join(review['personas'])}; plus section-by-section close "
                     f"reading, deterministic checks, and duplicate consolidation "
                     f"({review.get('n_raw_findings', 0)} raw findings → {len(ai_items)} items).")
        failed = [p for p in review.get("passes", []) if not p.get("ok")]
        if failed:
            L.append(f"- {len(failed)} pass(es) failed and are missing from this report: "
                     + "; ".join(f"{p['name']} ({(p.get('error') or '')[:80]})" for p in failed[:6]))
        for w in review.get("warnings") or []:
            L.append(f"- Note: {w}")
        L.append("- Effort estimates are author working hours. The bottom-up totals simply add up per-item "
                 "ranges (overlapping fixes are double-counted); the headline estimate is the model's holistic "
                 "judgement, which accounts for overlap.")
        L.append("")
    return "\n".join(L)


def write_report(project, out_path: str | Path | None = None, fmt: str = "docx") -> Path:
    """Write the review report next to the project (or to out_path). Returns
    the path written. fmt: 'docx' or 'md'. A Markdown copy is always kept."""
    md = build_report_markdown(project)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if out_path is None:
        project.reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = project.reports_dir / f"{project.slug}-review-{stamp}.{fmt}"
    out = Path(out_path)
    if out.suffix.lower() == ".md" or fmt == "md":
        out = out.with_suffix(".md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    return markdown_to_docx(md, out)
