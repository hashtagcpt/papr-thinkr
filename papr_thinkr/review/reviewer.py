"""Full manuscript review.

Two modes, chosen automatically:

* first-draft  -- no reviewer comments: papr-thinkr acts as a panel of harsh
  referees (section-by-section close reading, five specialist personas, a
  code/data audit, deterministic checks), consolidates and de-duplicates their
  findings, and writes an editor-style meta-review with an overall effort
  estimate and confidence.

* revision     -- reviewer comments exist: every comment is triaged (is the
  reviewer right? address / partially / clarify / rebut, effort in hours with
  a confidence, a drafted rebuttal where pushback is warranted), cross-reviewer
  conflicts and duplicates are found, and -- unless disabled -- the same
  independent critique as first-draft mode is run to catch what the reviewers
  missed.

Everything is written back into the project (AI critique items become
trackable comments, triage is attached to reviewer comments) and summarised in
project.state["review"], from which report/ builds the .docx report.
"""
from __future__ import annotations

import concurrent.futures as cf
import re
import time
from pathlib import Path

from .. import prompts
from ..inputs import code_and_data
from ..inputs.chunking import chunk_manuscript, fit_text, outline, review_units
from ..inputs.documents import manuscript_review_text, read_manuscript
from ..inputs.figures import collect_figures, read_code_texts
from ..inputs.line_refs import annotate_comments, refs_block
from ..inputs.review_letters import USER_FIELDS, merge_preserving_user_work
from ..llm import BaseLLM, CallContext, LLMCancelled, LLMError, current_context, use_context
from ..revision.cache import ReviewCache
from ..revision.provenance import capture_snapshot
from . import checks
from .effort import aggregate_effort, as_hours, clamp01, normalize_effort
from .findings import SEVERITY_RANK, dedupe, merge_group, norm_text, normalize_item, verify_quote


DEPTHS = ("quick", "standard", "thorough")
_FIGURE_REF_RE = re.compile(r"\bfig(?:ure)?s?\.?\s*(\d{1,2})", re.I)
_MISREAD_TYPES = ("none", "missed_existing_text", "misread", "terminology", "wrong_premise", "different_field_norms")


class _Pass:
    def __init__(self, name: str, kind: str, messages: list[dict], persona: str, schema: dict,
                 temperature: float = 0.3):
        self.name, self.kind, self.messages, self.persona = name, kind, messages, persona
        self.schema, self.temperature = schema, temperature
        self.result = None
        self.error: str | None = None
        self.seconds = 0.0


class Reviewer:
    def __init__(self, llm: BaseLLM, project, progress=None, depth: str | None = None,
                 personas: list[str] | None = None, independent: bool | None = None, force: bool = False):
        self.llm = llm
        self.project = project
        self.progress = progress or (lambda msg, frac=None: None)
        rc = project.config.get("review") or {}
        self.depth = depth or rc.get("depth") or "thorough"
        if self.depth not in DEPTHS:
            self.depth = "thorough"
        chosen = personas or rc.get("personas") or prompts.DEFAULT_PERSONAS
        self.personas = [p for p in chosen if p in prompts.PERSONAS] or list(prompts.DEFAULT_PERSONAS)
        self.independent = rc.get("independent_review", True) if independent is None else independent
        self.think = (project.config.get("llm") or {}).get("think")
        self.warnings: list[str] = []
        self.passes: list[_Pass] = []
        self.ctx = current_context() or CallContext()
        self.force = force

    def _json(self, messages, **kwargs):
        return self.cache.chat_json(messages, **kwargs)

    # ------------------------------------------------------------------ utils
    def _say(self, msg: str, frac: float | None = None):
        self.progress(msg, frac)

    def _system(self) -> str:
        cfg = self.project.config
        journal = (cfg.get("journal") or "").strip()
        venue = f"for the journal {journal}" if journal else "for a leading peer-reviewed journal"
        fld = (cfg.get("field") or "").strip() or "this field"
        return prompts.REVIEWER_SYSTEM.format(venue_clause=venue, field=fld)

    def _run_passes(self, passes: list[_Pass], label: str, frac0: float, frac1: float) -> None:
        if not passes:
            return
        total = len(passes)
        done = 0
        ctx = self.ctx

        def work(p: _Pass):
            use_context(ctx)
            t0 = time.monotonic()
            try:
                roles = (("manuscript", "reviews", "submitted", "figure", "code", "data")
                         if p.kind != "critique" or p.persona.startswith(("Code", "Figure")) else ("manuscript",))
                p.result = self._json(p.messages, temperature=p.temperature, roles=roles,
                                     json_schema=p.schema, think=self.think,
                                     cache_if=lambda r: isinstance(r, list) and (p.kind == "critique" or
                                     {str(it.get("comment_id", "")).strip("[] ") for it in r if isinstance(it, dict)} ==
                                     {c["id"] for c in p.batch}))
                if p.kind == "critique" and p.result == []:
                    # Models occasionally answer [] on a run where they'd normally find
                    # plenty; one nudge recovers a whole pass's worth of findings.
                    p.result = self._json(p.messages + [
                        {"role": "assistant", "content": "[]"},
                        {"role": "user", "content": "You returned no items. Look again carefully at everything "
                                                    "provided and list every genuine problem; return [] only if "
                                                    "there is truly nothing wrong."}],
                        temperature=p.temperature, json_schema=p.schema, think=self.think, roles=roles)
            except LLMCancelled:
                raise
            except LLMError as e:
                p.error = str(e)
            p.seconds = time.monotonic() - t0
            return p

        workers = max(1, min(self.llm.max_parallel, total))
        self._say(f"{label}: {total} model call(s){' in parallel' if workers > 1 else ''}...", frac0)
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, p) for p in passes]
            try:
                for fut in cf.as_completed(futures):
                    p = fut.result()
                    done += 1
                    n = len(p.result) if isinstance(p.result, list) else ("ok" if p.result else 0)
                    status = f"failed: {p.error[:160]}" if p.error else f"{n} item(s)"
                    self._say(f"  [{done}/{total}] {p.name}: {status} ({p.seconds:.0f}s)",
                              frac0 + (frac1 - frac0) * done / total)
            except LLMCancelled:
                for f in futures:
                    f.cancel()
                raise
        self.passes.extend(passes)

    # --------------------------------------------------------------- material
    def _load(self):
        cfg = self.project.config
        path = self.project.manuscript_path()
        self.ms_path = path
        self.ms_ext = path.suffix.lower()
        self.ms_full = read_manuscript(path)
        self.ms_text = manuscript_review_text(path)
        self.ms_norm = norm_text(self.ms_full)
        self.chunks = chunk_manuscript(self.ms_text, self.ms_ext)
        self.budget = self.llm.context_chars
        self.material, self.material_note = fit_text(self.ms_text, self.chunks, int(self.budget * 0.8))
        self.fits_whole = self.material_note is None or self.material_note.startswith("References")
        if self.material_note:
            self.warnings.append(self.material_note)
        title = (cfg.get("title") or "").strip()
        self.material_block = prompts.MATERIAL_BLOCK.format(
            title_clause=f' "{title}"' if title else "", manuscript=self.material,
            note=f"[Note: {self.material_note}]\n" if self.material_note else "")
        self.dossier = None
        if cfg.get("code_paths") or cfg.get("data_paths"):
            self._say("Reading code and data files...")
            self.dossier = code_and_data.build_dossier(cfg.get("code_paths") or [], cfg.get("data_paths") or [],
                                                   budget_chars=min(int(self.budget * 0.45), 150_000))
            for n in self.dossier.notes:
                self.warnings.append(n)
            self._say(f"  {len(self.dossier.files)} file(s), {len(self.dossier.data_summaries)} data summar"
                      f"{'y' if len(self.dossier.data_summaries) == 1 else 'ies'}, "
                      f"{len(self.dossier.findings)} static finding(s)")
        self.figures, self.unused_figure_outputs = [], []
        try:
            fig_dir = cfg.get("figures_dir")
            self.figures, self.unused_figure_outputs = collect_figures(
                path, read_code_texts(cfg.get("code_paths") or []), figures_dir=Path(fig_dir) if fig_dir else None,
                cache_dir=self.project.dir / "figure-cache", manuscript_text=self.ms_text)
        except Exception as e:      # figures are a bonus: never fail a review over them
            self.warnings.append(f"Figure analysis skipped: {type(e).__name__}: {e}")
        self.figure_norm = norm_text("\n".join(f.describe() for f in self.figures))
        if self.figures:
            n_img = sum(1 for f in self.figures if f.image)
            self._say(f"  {len(self.figures)} figure(s): {sum(1 for f in self.figures if f.code)} matched to the "
                      f"code that draws them, {n_img} image(s) ready"
                      + (" for the model to look at" if n_img and self.llm.supports_images else
                         " (this model can't see images; it will judge captions and code)" if n_img else ""))

    # -------------------------------------------------------------- critique
    def _critique_passes(self) -> list[_Pass]:
        system = self._system()
        passes: list[_Pass] = []

        def mk(name, task, persona, prefix=None):
            content = (prefix if prefix is not None else self.material_block) + "\n" + task
            return _Pass(name, "critique", [{"role": "system", "content": system},
                                            {"role": "user", "content": content}],
                         persona, prompts.CRITIQUE_SCHEMA)

        if self.depth == "quick":
            passes.append(mk("Whole-paper review", prompts.CRITIQUE_GENERAL_TASK, "General referee"))
        else:
            # Section-by-section close reading
            max_unit = max(4000, min(30000, self.budget // 3))
            units = review_units(self.chunks, min_chars=2000, max_chars=max_unit)
            if len(units) > 30:
                units = review_units(self.chunks, min_chars=max(6000, len(self.ms_text) // 30), max_chars=max_unit)
            if self.fits_whole:
                prefix = self.material_block
            else:
                opening = self.chunks[0].text[:4000] if self.chunks else self.ms_text[:4000]
                prefix = prompts.SECTION_CONTEXT_BLOCK.format(outline=outline(self.chunks), opening=opening)
            for u in units:
                task = prompts.CRITIQUE_SECTION_TASK.format(heading=u.heading, section_text=u.text)
                passes.append(mk(f"Section: {u.heading[:60]}", task, f"Section reader ({u.heading[:40]})", prefix))
            if self.depth == "standard":
                passes.append(mk("Whole-paper review", prompts.CRITIQUE_GENERAL_TASK, "General referee"))
            else:
                for key in self.personas:
                    p = prompts.PERSONAS[key]
                    passes.append(mk(f"Referee: {p['name']}", prompts.CRITIQUE_WHOLE_TASK.format(focus=p["focus"]),
                                     p["name"]))
        if self.dossier and self.dossier.text:
            focus = "methods statistical analysis software code data sample size results"
            mat, note = fit_text(self.ms_text, self.chunks, max(8000, int(self.budget * 0.8) - len(self.dossier.text)),
                                 focus_query=focus)
            block = prompts.MATERIAL_BLOCK.format(title_clause="", manuscript=mat,
                                                  note=f"[Note: {note}]\n" if note else "")
            task = prompts.CODE_AUDIT_TASK.format(dossier="CODE AND DATA PROVIDED:\n" + self.dossier.text)
            passes.append(mk("Code & data audit", task, "Code & data auditor", block))
        return passes

    def _figure_passes(self) -> list[_Pass]:
        """One figure-referee call per figure: caption + citing text + plotting
        code + the data it reads (+ the image, for models that can see)."""
        passes = []
        system = self._system()
        for fig in self.figures[:15]:
            if not (fig.caption or fig.code or fig.image):
                continue
            data = ""
            if self.dossier and fig.code:
                reads = {r.lower() for c in fig.code for r in c.get("reads", [])}
                blocks = [code_and_data.format_data_summary(s) for s in self.dossier.data_summaries
                          if Path(s["file"]).name.lower() in reads]
                if blocks:
                    data = "DATA READ BY THIS CODE:\n" + "\n\n".join(blocks)[:6000]
            mat, note = fit_text(self.ms_text, self.chunks, max(6000, int(self.budget * 0.4)),
                                 focus_query=fig.caption + " " + " ".join(fig.mentions))
            sees = bool(fig.image) and self.llm.supports_images
            task = prompts.FIGURE_AUDIT_TASK.format(
                image_note=("The figure image is attached: look at it carefully." if sees else
                            "No image of this figure is available to you; judge it from the caption, text and code."),
                and_image=" and what the image shows" if sees else "", figure=fig.describe(), data=data)
            msg = {"role": "user", "content": prompts.MATERIAL_BLOCK.format(
                title_clause="", manuscript=mat, note=f"[Note: {note}]\n" if note else "") + "\n" + task}
            if sees:
                msg["images"] = [fig.image]
            passes.append(_Pass(f"Figure {fig.number}" + (" (with image)" if sees else ""), "critique",
                                [{"role": "system", "content": system}, msg], "Figure referee",
                                prompts.CRITIQUE_SCHEMA))
        return passes

    def _collect_items(self, passes: list[_Pass]) -> list[dict]:
        items: list[dict] = []
        dossier_norm = norm_text(self.dossier.text) if self.dossier else ""
        for p in passes:
            if p.kind != "critique" or not isinstance(p.result, list):
                continue
            for raw in p.result:
                it = normalize_item(raw, p.persona)
                if it is None:
                    continue
                hays = [dossier_norm] if p.persona.startswith("Code") else [self.ms_norm]
                if p.persona.startswith("Figure"):
                    hays.append(self.figure_norm)
                verdicts = [verify_quote(it["quote"], h) for h in hays if h]
                it["quote_verified"] = (True if True in verdicts else (None if not verdicts or None in verdicts
                                                                       else False))
                if it["quote_verified"] is False:
                    # Possibly hallucinated quote: keep the item, but trust it less.
                    it["confidence"] = round(it["confidence"] * 0.8, 2)
                items.append(it)
        return items

    def _deterministic_items(self) -> list[dict]:
        raw = checks.check_manuscript(self.ms_full, str(self.ms_path))
        if self.dossier:
            raw += self.dossier.findings
        raw += checks.check_figures(self.figures, bool(self.project.config.get("code_paths")))
        out = []
        for r in raw:
            it = normalize_item(r, r.get("persona", "Automated checks"))
            if it:
                it["quote_verified"] = None
                it["automated"] = True
                out.append(it)
        return out

    def _llm_dedupe(self, items: list[dict]) -> list[dict]:
        if len(items) < 12 or self.depth == "quick":
            return items
        lines = [f"{i + 1}. [{it['severity']}|{it['category']}] {it['title']} -- "
                 f"{(it['location'] or '')[:40]} -- {it['issue'][:170]}" for i, it in enumerate(items)]
        listing = "\n".join(lines)
        if len(listing) > self.budget:
            return items
        self._say("Consolidating duplicate findings across referees...")
        try:
            result = self._json([
                {"role": "system", "content": "You consolidate peer-review findings and respond only in JSON."},
                {"role": "user", "content": prompts.DEDUP_TASK.format(items=listing)},
            ], json_schema=prompts.DEDUP_SCHEMA, temperature=0.1, think=self.think)
        except LLMCancelled:
            raise
        except LLMError as e:
            self.warnings.append(f"LLM de-duplication skipped: {e}")
            return items
        groups = result.get("groups") if isinstance(result, dict) else None
        if not isinstance(groups, list):
            return items
        used: set[int] = set()
        merged: list[dict] = []
        for g in groups:
            if not isinstance(g, list):
                continue
            idx = sorted({int(i) - 1 for i in g if isinstance(i, (int, float)) and 1 <= int(i) <= len(items)} - used)
            if len(idx) < 2:
                continue
            used.update(idx)
            merged.append(merge_group([items[i] for i in idx]))
        merged.extend(it for i, it in enumerate(items) if i not in used)
        return merged

    # ---------------------------------------------------------------- triage
    def _reviewer_comments(self) -> list[dict]:
        return [c for c in self.project.state["comments"] if c.get("origin", "reviewer") == "reviewer"]

    def _comments_prefix(self, comments: list[dict]) -> tuple[str, str]:
        """(system, prefix) shared by the decode and triage calls, so a local
        model re-uses its prompt cache between them."""
        if getattr(self, "_cprefix", None):
            return self._cprefix
        all_lines = "\n".join(f"[{c['id']}] ({c['reviewer']}) {' '.join(c['comment_text'].split())[:320]}"
                              for c in comments)
        ctx_block = prompts.TRIAGE_CONTEXT_BLOCK.format(all_comments=all_lines)
        extra = ""
        if self.dossier and self.dossier.files:
            extra = "\nCODE/DATA FILES PROVIDED WITH THE MANUSCRIPT:\n" + "\n".join(
                f"  {f.rel} [{f.kind}]" for f in self.dossier.files[:80]) + "\n"
        budget = int(self.budget * 0.8) - len(ctx_block) - len(extra) - 16000
        mat, note = fit_text(self.ms_text, self.chunks, max(8000, budget),
                             focus_query=" ".join(c["comment_text"] for c in comments)[:20000])
        if note:
            self.warnings.append(f"Reviewer comments: {note}")
        prefix = prompts.MATERIAL_BLOCK.format(title_clause="", manuscript=mat,
                                               note=f"[Note: {note}]\n" if note else "") + "\n" + ctx_block + extra
        system = (self._system() + "\nIn this task you are on the AUTHORS' side: an experienced, candid co-author "
                  "helping them understand and answer their reviews, who knows when to concede and when to push back.")
        self._cprefix = (system, prefix)
        return self._cprefix

    def _comment_block(self, c: dict, decoded: dict | None = None) -> str:
        out = (f"[{c['id']}] ({c['reviewer']}{', ' + c['location_ref'] if c.get('location_ref') else ''}):\n"
               f"{c['comment_text']}")
        rb = refs_block(c)
        if rb:
            out += "\n" + rb
        for n in c.get("figure_refs") or []:
            fig = next((f for f in self.figures if f.number == n), None)
            if fig:
                out += "\n  (The comment concerns this figure:\n   " + fig.describe(code_chars=1500).replace(
                    "\n", "\n   ") + ")"
        if decoded:
            out += f"\n  (First-pass decoding: {decoded['plain_language']}"
            if decoded["misunderstanding"] != "none":
                out += (f" Possible misunderstanding ({decoded['misunderstanding']}): "
                        f"{decoded['misunderstanding_explanation']} Manuscript text: \u201c{decoded['manuscript_evidence']}\u201d"
                        + ("" if decoded.get("evidence_verified") is not False else " [quote NOT found in manuscript]"))
            out += ")"
        return out

    def _comment_passes(self, comments: list[dict], kind: str, decoded: dict | None = None) -> list[_Pass]:
        if not comments:
            return []
        system, prefix = self._comments_prefix(comments)
        if kind == "decode":
            size, task, schema, label = (4 if self.llm.is_local else 8), prompts.DECODE_TASK, prompts.DECODE_SCHEMA, "Decode"
        else:
            size, task, schema, label = (5 if self.llm.is_local else 10), prompts.TRIAGE_TASK, prompts.TRIAGE_SCHEMA, "Triage"
        passes = []
        for i in range(0, len(comments), size):
            batch = comments[i:i + size]
            block = "\n\n".join(self._comment_block(c, (decoded or {}).get(c["id"])) for c in batch)
            p = _Pass(f"{label} comments {i + 1}-{i + len(batch)}", kind,
                      [{"role": "system", "content": system},
                       {"role": "user", "content": prefix + "\n" + task.format(batch=block)}],
                      kind, schema, temperature=0.2)
            p.batch = batch
            passes.append(p)
        return passes

    def _triage_passes(self, comments: list[dict], decoded: dict | None = None) -> list[_Pass]:
        return self._comment_passes(comments, "triage", decoded)

    @staticmethod
    def _match_results(p: _Pass) -> dict[str, dict]:
        batch_ids = [c["id"] for c in p.batch]
        results = [r for r in (p.result or []) if isinstance(r, dict)] if isinstance(p.result, list) else []
        by_id = {}
        for r in results:
            rid = str(r.get("comment_id") or "").strip().strip("[]")
            if rid in batch_ids:
                by_id[rid] = r
        return by_id

    def _normalize_decode(self, r: dict) -> dict:
        interps = [{"reading": str(i.get("reading", "")).strip(), "likelihood": round(clamp01(i.get("likelihood"), 0.5), 2)}
                   for i in (r.get("interpretations") or []) if isinstance(i, dict) and str(i.get("reading", "")).strip()]
        evidence = str(r.get("manuscript_evidence") or "").strip().strip("\"\u201c\u201d")
        sat = r.get("what_would_satisfy") if isinstance(r.get("what_would_satisfy"), list) else []
        return {
            "plain_language": str(r.get("plain_language") or "").strip(),
            "underlying_concern": str(r.get("underlying_concern") or "").strip(),
            "what_would_satisfy": [str(x).strip() for x in sat if str(x).strip()][:5],
            "clarity": r.get("clarity") if r.get("clarity") in ("clear", "somewhat_unclear", "unclear") else "clear",
            "interpretations": sorted(interps, key=lambda i: -i["likelihood"])[:3],
            "misunderstanding": r.get("misunderstanding") if r.get("misunderstanding") in _MISREAD_TYPES else "none",
            "misunderstanding_explanation": str(r.get("misunderstanding_explanation") or "").strip(),
            "manuscript_evidence": evidence,
            "evidence_verified": verify_quote(evidence, self.ms_norm) if evidence else None,
            "evidence_location": str(r.get("evidence_location") or "").strip(),
            "clarifying_edit": str(r.get("clarifying_edit") or "").strip(),
            "tone": r.get("tone") if r.get("tone") in ("constructive", "neutral", "curt", "hostile") else "neutral",
            "neutral_rewrite": str(r.get("neutral_rewrite") or "").strip(),
            "clarification_request": str(r.get("clarification_request") or "").strip(),
            "generated_at": time.time(),
        }

    def _collect_decode(self, passes: list[_Pass]) -> dict[str, dict]:
        out = {}
        for p in passes:
            for cid, r in self._match_results(p).items():
                out[cid] = self._normalize_decode(r)
        return out

    @staticmethod
    def _normalize_triage(r: dict) -> dict:
        stance = r.get("stance") if r.get("stance") in ("address", "address_partially", "clarify", "rebut") else "address"
        agreement = r.get("agreement") if r.get("agreement") in ("agree", "partially_agree", "disagree") else "agree"
        changes = r.get("required_changes") if isinstance(r.get("required_changes"), list) else []
        rebuttal = str(r.get("rebuttal") or "").strip() if stance != "address" else ""
        return {
            "category": r.get("category") if r.get("category") in prompts.CATEGORIES else "other",
            "agreement": agreement,
            "agreement_confidence": round(clamp01(r.get("agreement_confidence"), 0.6), 2),
            "stance": stance,
            "already_addressed": r.get("already_addressed") if r.get("already_addressed") in ("yes", "partly", "no") else "no",
            "evidence": str(r.get("evidence") or "").strip(),
            "required_changes": [str(x).strip() for x in changes if str(x).strip()][:12],
            "effort": normalize_effort(r),
            "rebuttal": rebuttal,
            "rebuttal_confidence": round(clamp01(r.get("rebuttal_confidence"), 0.0), 2) if rebuttal else 0.0,
            "rationale": str(r.get("rationale") or "").strip(),
            "generated_at": time.time(),
        }

    def _collect_triage(self, passes: list[_Pass]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in passes:
            if p.kind == "triage":
                for cid, r in self._match_results(p).items():
                    out[cid] = self._normalize_triage(r)
        return out

    def _cross_comment(self, comments: list[dict]) -> dict | None:
        if len(comments) < 2:
            return None
        per = max(150, min(800, int(self.budget * 0.8) // max(1, len(comments)) - 40))
        listing = "\n".join(f"[{c['id']}] ({c['reviewer']}) {' '.join(c['comment_text'].split())[:per]}"
                            for c in comments)
        self._say("Looking for conflicts and duplicates between reviewers...")
        try:
            r = self._json([
                {"role": "system", "content": "You analyse peer-review comments and respond only in JSON."},
                {"role": "user", "content": prompts.CROSS_COMMENT_TASK.format(comments=listing)},
            ], json_schema=prompts.CROSS_COMMENT_SCHEMA, temperature=0.2, think=self.think)
        except LLMCancelled:
            raise
        except LLMError as e:
            self.warnings.append(f"Cross-reviewer analysis failed: {e}")
            return None
        if not isinstance(r, dict):
            return None
        valid = {c["id"] for c in comments}

        def clean(lst, keys):
            out = []
            for g in lst or []:
                if not isinstance(g, dict):
                    continue
                ids = [str(i).strip("[] ") for i in (g.get("comment_ids") or []) if str(i).strip("[] ") in valid]
                if ids:
                    out.append({"comment_ids": ids, **{k: str(g.get(k) or "") for k in keys}})
            return out

        profiles = []
        for g in r.get("reviewers") or []:
            if isinstance(g, dict) and g.get("reviewer"):
                profiles.append({"reviewer": str(g["reviewer"]), "expertise_guess": str(g.get("expertise_guess") or ""),
                                 "main_concerns": [str(x) for x in (g.get("main_concerns") or [])][:5],
                                 "tone": str(g.get("tone") or ""), "how_to_respond": str(g.get("how_to_respond") or "")})
        return {"conflicts": clean(r.get("conflicts"), ["description", "suggested_resolution"]),
                "duplicates": clean(r.get("duplicates"), ["theme"]),
                "themes": clean(r.get("themes"), ["theme"]),
                "reviewers": profiles}

    # ------------------------------------------------------------------ meta
    def _meta(self, items: list[dict], triage: dict[str, dict], comments: list[dict], bottom_up: dict | None,
              decoded_for_meta: dict | None = None) -> dict:
        lines = []
        for it in items[:160]:
            e = it["effort"]
            lines.append(f"{it['id']} | {it['severity']} | {it['category']} | {it['title']} | "
                         f"{e['hours_low']:g}-{e['hours_high']:g}h | {it['confidence']:.0%}")
        if len(items) > 160:
            lines.append(f"... plus {len(items) - 160} further minor/editorial items")
        triage_block = ""
        if comments:
            tl = []
            for c in comments:
                t = triage.get(c["id"])
                if not t:
                    continue
                e = t["effort"]
                dc = (decoded_for_meta or {}).get(c["id"]) or {}
                mis = f" | misreads paper: {dc['misunderstanding']}" if dc.get("misunderstanding", "none") != "none" else ""
                tl.append(f"{c['id']} | {c['reviewer']} | stance={t['stance']}{mis} | reviewer is {t['agreement']} "
                          f"| {e['hours_low']:g}-{e['hours_high']:g}h | {' '.join(c['comment_text'].split())[:160]}")
            triage_block = "REVIEWER COMMENTS AND PLANNED RESPONSES (id | reviewer | stance | assessment | hours | comment):\n" + "\n".join(tl) + "\n"
        bu = (f"{bottom_up['hours_low']:g}-{bottom_up['hours_high']:g} hours (likely ~{bottom_up['hours_likely']:g} h)"
              if bottom_up else "n/a")
        journal = (self.project.config.get("journal") or "").strip()
        opening = self.chunks[0].text[:3500] if self.chunks and len(self.chunks[0].text) > 400 else self.ms_text[:4000]
        prompt = prompts.META_REVIEW_TASK.format(
            venue_clause=f" submitted to {journal}" if journal else "",
            revision_clause=" together with the journal reviewers' comments and the authors' planned responses" if comments else "",
            opening=opening, items="\n".join(lines) or "(no findings)", triage_block=triage_block, bottom_up=bu,
            rebuttal_strategy_hint=("how to handle the comments recommended for rebuttal/clarification, and how to "
                                    "frame the response letter" if comments else "empty string (no reviewer comments yet)"))
        self._say("Writing the overall assessment...")
        try:
            r = self._json([{"role": "system", "content": self._system()},
                                    {"role": "user", "content": prompt}],
                                   json_schema=prompts.META_REVIEW_SCHEMA, temperature=0.3, think=self.think)
        except LLMCancelled:
            raise
        except LLMError as e:
            self.warnings.append(f"Overall assessment failed: {e}")
            r = {}
        if not isinstance(r, dict):
            r = {}
        lo, hi = as_hours(r.get("effort_hours_low")), as_hours(r.get("effort_hours_high"))
        if lo is None or hi is None:
            lo = bottom_up["hours_low"] if bottom_up else 0
            hi = bottom_up["hours_high"] if bottom_up else 0
        if lo > hi:
            lo, hi = hi, lo
        prios = []
        for p in r.get("top_priorities") or []:
            if isinstance(p, dict) and p.get("title"):
                prios.append({"title": str(p["title"]), "why": str(p.get("why") or ""),
                              "item_ids": [str(x) for x in (p.get("item_ids") or [])][:10]})
        return {
            "summary": str(r.get("summary") or "").strip(),
            "strengths": [str(s) for s in (r.get("strengths") or []) if str(s).strip()],
            "key_weaknesses": [str(s) for s in (r.get("key_weaknesses") or []) if str(s).strip()],
            "recommendation": r.get("recommendation") if r.get("recommendation") in
            ("accept", "minor_revision", "major_revision", "reject") else None,
            "recommendation_confidence": round(clamp01(r.get("recommendation_confidence"), 0.5), 2),
            "effort_level": r.get("effort_level") if r.get("effort_level") in
            ("light", "moderate", "substantial", "extensive") else (bottom_up or {}).get("level"),
            "effort_hours_low": round(lo, 1),
            "effort_hours_high": round(hi, 1),
            "effort_confidence": round(clamp01(r.get("effort_confidence"), (bottom_up or {}).get("confidence", 0.5)), 2),
            "effort_rationale": str(r.get("effort_rationale") or "").strip(),
            "top_priorities": prios,
            "rebuttal_strategy": str(r.get("rebuttal_strategy") or "").strip(),
            "likely_outcome": str(r.get("likely_outcome") or "").strip(),
            "ok": bool(r),
        }

    # ------------------------------------------------------------------- run
    def run(self) -> dict:
        t_start = time.time()
        self.snapshot = capture_snapshot(self.project, "Full review")
        self.cache = ReviewCache(self.project, self.llm, self.snapshot, force=self.force)
        self._load()
        comments = self._reviewer_comments()
        mode = "revision" if comments else "first-draft"
        self._say(f"Mode: {mode}. Depth: {self.depth}. Backend: {self.llm.display_name}. "
                  f"Manuscript: {len(self.ms_text):,} chars"
                  f"{' (whole text fits in context)' if self.fits_whole else ' (too long for one prompt; sections will be fitted)'}.",
                  0.02)

        if comments:
            n_refs = annotate_comments(comments, self.project.config.get("submitted_pdf"))
            if n_refs:
                self._say(f"Resolved the line/page numbers cited in {n_refs} comment(s) against the submitted PDF.")
            elif self.project.config.get("submitted_pdf"):
                self.warnings.append("The submitted PDF has no readable line numbers, so 'line N' references "
                                     "could not be resolved.")
            for c in comments:
                c["figure_refs"] = sorted({int(n) for n in _FIGURE_REF_RE.findall(
                    f"{c.get('location_ref') or ''} {c['comment_text']}")
                                           if any(f.number == int(n) for f in self.figures)})
        critique_passes = ((self._critique_passes() + self._figure_passes())
                           if (mode == "first-draft" or self.independent) else [])
        decode_passes = self._comment_passes(comments, "decode")
        self._run_passes(decode_passes, "Working out what each reviewer actually means", 0.04,
                         0.18 if critique_passes else 0.45)
        decoded = self._collect_decode(decode_passes)
        triage_passes = self._triage_passes(comments, decoded)
        self._run_passes(triage_passes, "Triaging reviewer comments", 0.18 if critique_passes else 0.45,
                         0.33 if critique_passes else 0.8)
        triage = self._collect_triage(triage_passes)
        missing = [c["id"] for c in comments if c["id"] not in triage]
        if missing and triage_passes:
            self.warnings.append(f"{len(missing)} reviewer comment(s) could not be triaged: {', '.join(missing[:10])}")
        cross = self._cross_comment(comments) if comments else None

        self._run_passes(critique_passes, "Independent critique", 0.36 if comments else 0.05, 0.85)
        items = self._collect_items(critique_passes) + self._deterministic_items()
        n_raw = len(items)
        items = dedupe(items)
        items = self._llm_dedupe(items)
        items.sort(key=lambda it: (SEVERITY_RANK[it["severity"]], -it["confidence"], it["title"]))
        self._assign_ids(items)
        self._say(f"Consolidated {n_raw} raw finding(s) into {len(items)} item(s).", 0.88)

        # Effort: reviewer comments (what must be done) and independent items
        # (what a harsh referee would also demand), reported separately.
        eff_reviewers = aggregate_effort([t["effort"] for t in triage.values()])
        eff_indep = aggregate_effort([it["effort"] for it in items])
        eff_indep_major = aggregate_effort([it["effort"] for it in items if it["severity"] in ("critical", "major")])
        bottom_up = eff_reviewers if comments else eff_indep
        meta = self._meta(items, triage, comments, bottom_up, decoded)

        review = {
            "generated_at": time.time(),
            "snapshot_id": self.snapshot["id"],
            "cache": self.cache.summary(),
            "mode": mode,
            "depth": self.depth,
            "backend": self.llm.display_name,
            "personas": [prompts.PERSONAS[p]["name"] for p in self.personas] if self.depth == "thorough" else [],
            "independent_review": bool(critique_passes),
            "manuscript": {"path": str(self.ms_path), "chars": len(self.ms_text), "fits_whole": self.fits_whole,
                           "material_note": self.material_note,
                           "sections": [c.heading for c in self.chunks if c.kind != "references"][:80]},
            "overall": meta,
            "effort": {"reviewer_comments": eff_reviewers, "independent": eff_indep,
                       "independent_major": eff_indep_major},
            "cross_comment": cross,
            "code_audit": self.dossier.as_dict() if self.dossier else None,
            "passes": [{"name": p.name, "ok": p.error is None, "error": p.error,
                        "n_items": len(p.result) if isinstance(p.result, list) else 0,
                        "seconds": round(p.seconds, 1)} for p in self.passes],
            "n_raw_findings": n_raw,
            "usage": self.ctx.as_dict(),
            "warnings": self.warnings + list(getattr(self.llm, "warnings", []) or []),
            "elapsed_seconds": round(time.time() - t_start, 1),
        }
        failed = [p for p in self.passes if p.error]
        if self.passes and len(failed) == len(self.passes):
            raise LLMError("Every model call failed. First error: " + (failed[0].error or "unknown"))
        review["figures"] = [f.as_dict() for f in self.figures]
        review["unused_figure_outputs"] = [{k: o[k] for k in ("file", "line", "target")}
                                           for o in self.unused_figure_outputs][:30]
        review["n_misread"] = sum(1 for d in decoded.values() if d["misunderstanding"] != "none")
        review["n_unclear"] = sum(1 for d in decoded.values() if d["clarity"] != "clear")
        self._persist(items, triage, review, decoded, comments)
        self._say(f"Review complete in {review['elapsed_seconds'] / 60:.1f} min "
                  f"({self.ctx.calls} model calls, {len(failed)} failed).", 1.0)
        return review

    @staticmethod
    def _ai_text(it: dict) -> str:
        return it["title"].rstrip(".") + ". " + it["issue"]

    def _assign_ids(self, items: list[dict]) -> None:
        """Re-runs keep the id of an item that matches one from the previous
        run (so notes, patches and chat threads stay attached); genuinely new
        items get fresh ids that never reuse an old one."""
        with self.project.lock:
            self.project.reload()
            old_ai = [c for c in self.project.state["comments"] if c.get("origin") == "ai-review"]
        tmp = [{"id": f"tmp-{i}", "comment_text": self._ai_text(it)} for i, it in enumerate(items)]
        matched = merge_preserving_user_work([dict(c) for c in old_ai], tmp, threshold=0.7)
        taken = {c["id"] for c in old_ai}
        n = 0
        for it, m in zip(items, matched):
            if not m["id"].startswith("tmp-"):
                it["id"] = m["id"]
                continue
            while True:
                n += 1
                cand = f"ai-{n:03d}"
                if cand not in taken:
                    break
            taken.add(cand)
            it["id"] = cand

    def _persist(self, items: list[dict], triage: dict[str, dict], review: dict,
                 decoded: dict[str, dict] | None = None, comments_in: list[dict] | None = None) -> None:
        new_ai = []
        for n, it in enumerate(items, 1):
            new_ai.append({
                "id": it["id"], "origin": "ai-review", "source": "papr-review",
                "reviewer": "papr-thinkr \u00b7 " + it["personas"][0], "order": n,
                "location_ref": it.get("location"), "comment_text": self._ai_text(it), "severity": it["severity"],
                "status": "unaddressed", "status_evidence": "", "suggested_action": it.get("suggestion", ""),
                "author_notes": "", "response_text": "", "patch_ids": [], "critique": it,
                "assessment_snapshot_id": self.snapshot["id"],
            })
        with self.project.mutate():
            comments = self.project.state["comments"]
            fresh = {c["id"]: c for c in (comments_in or [])}
            for c in comments:
                if c["id"] in triage:
                    keep = (c.get("triage") or {}).get("stance") if (c.get("triage") or {}).get("stance_set_by_author") else None
                    c["triage"] = triage[c["id"]]
                    c["assessment_snapshot_id"] = self.snapshot["id"]
                    if keep:
                        c["triage"].update(stance=keep, stance_set_by_author=True)
                if c["id"] in (decoded or {}):
                    c["decoded"] = decoded[c["id"]]
                if c["id"] in fresh:
                    c["resolved_refs"] = fresh[c["id"]].get("resolved_refs") or []
                    c["figure_refs"] = fresh[c["id"]].get("figure_refs") or []
            old_ai = {c["id"]: c for c in comments if c.get("origin") == "ai-review"}
            for c in new_ai:
                old = old_ai.get(c["id"])
                if old:
                    for f in USER_FIELDS:
                        if f not in ("triage", "assessment_snapshot_id") and old.get(f) not in (None, "", [], "unaddressed"):
                            c[f] = old[f]
            new_ids = {c["id"] for c in new_ai}
            # keep old AI items the author already worked on, even if this run didn't re-find them
            kept = [dict(c, stale=True) for cid, c in old_ai.items() if cid not in new_ids and (
                c.get("author_notes") or c.get("response_text") or c.get("patch_ids")
                or c.get("status") not in (None, "unaddressed"))]
            reviewer = [c for c in comments if c.get("origin", "reviewer") != "ai-review"]
            self.project.state["comments"] = reviewer + new_ai + kept
            self.project.state["review"] = review


def run_review(llm: BaseLLM, project, progress=None, **kw) -> dict:
    return Reviewer(llm, project, progress=progress, **kw).run()
