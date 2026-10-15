"""Shared test helpers: a scripted fake LLM and an isolated projects folder.
Run the suite from the repository root with:  python -m unittest discover tests"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from papr_thinkr import project as project_mod, prompts  # noqa: E402
from papr_thinkr.inputs import documents, review_letters  # noqa: E402
from papr_thinkr.llm import BaseLLM  # noqa: E402

DEMO = ROOT / "examples" / "demo-paper"
HAS_PDFTOTEXT = bool(shutil.which("pdftotext"))
HAS_PDFTOPPM = bool(shutil.which("pdftoppm"))


class FakeLLM(BaseLLM):
    """Answers by recognising which schema was requested, the way the real
    pipeline uses them. Records every call for assertions."""
    backend = "fake"
    label = "Fake"
    is_local = True
    supports_native_schema = True

    def __init__(self, context_chars: int = 200_000, max_parallel: int = 1, vision: bool = False):
        super().__init__(None)
        self._ctx = context_chars
        self.max_parallel = max_parallel
        self.vision = vision
        self.calls: list[dict] = []

    @property
    def context_chars(self) -> int:
        return self._ctx

    @property
    def supports_images(self) -> bool:
        return self.vision

    def _chat(self, messages, temperature, json_schema, think):
        prompt = messages[-1]["content"]
        self.calls.append({"schema": json_schema, "prompt": prompt, "system": messages[0]["content"],
                           "images": messages[-1].get("images")})
        s = json_schema
        from papr_thinkr.review.scientific import SCIENTIFIC_SCHEMA
        from papr_thinkr.review.joint_plan import JOINT_PLAN_SCHEMA
        if s is SCIENTIFIC_SCHEMA:
            return json.dumps({"summary": "Independent scientific audit completed.", "findings": [], "links": [], "coverage_note": ""})
        if s is JOINT_PLAN_SCHEMA:
            return json.dumps({"summary": "Consider requests together.", "actions": [], "reviewer_coverage": []})
        if s is prompts.COMMENT_EXTRACTION_SCHEMA:
            src = prompt.split("SOURCE TEXT:\n---\n", 1)[1].rsplit("\n---", 1)[0]
            paras = [p.strip() for p in re.split(r"\n\s*\n", src) if len(p.strip()) > 20]
            return json.dumps([{"reviewer": "X", "location_ref": None, "comment_text": p, "severity": "major"}
                               for p in paras if not p.startswith("Dear")])
        if s is prompts.CRITIQUE_SCHEMA:
            return json.dumps([
                self._item("Impossible p-value", "statistics", "minor", "p = 0.000", 0.9),
                self._item("Unblinded refraction measurement", "methods", "major",
                           "The experimenter measuring refraction knew the group of each chick", 0.8),
                self._item("Made-up problem", "other", "minor", "this sentence is not in the paper at all ok", 0.5),
            ])
        if s is prompts.DECODE_SCHEMA:
            ids = re.findall(r"^\[([^\]]+)\]", prompt.split("COMMENTS TO DECODE:", 1)[1], re.M)
            out = []
            for i, cid in enumerate(ids):
                out.append({
                    "comment_id": cid, "plain_language": f"They want change {i}.", "underlying_concern": "Rigour.",
                    "what_would_satisfy": ["Do X", "Report Y"],
                    "clarity": "unclear" if i == 1 else "clear",
                    "interpretations": [{"reading": "Reading A", "likelihood": 0.6},
                                        {"reading": "Reading B", "likelihood": 0.4}] if i == 1 else [],
                    "misunderstanding": "missed_existing_text" if i == 0 else "none",
                    "misunderstanding_explanation": "The method is described in 2.4." if i == 0 else "",
                    "manuscript_evidence": "Choroidal thickness was measured on day 0 and day 7" if i == 0 else "",
                    "evidence_location": "Methods 2.4" if i == 0 else "",
                    "clarifying_edit": "Add a pointer to Section 2.4 in the Results." if i == 0 else "",
                    "tone": "hostile" if i == 2 else "neutral",
                    "neutral_rewrite": f"Please address point {i}.",
                    "clarification_request": "We understood the reviewer to ask A." if i == 1 else "",
                })
            return json.dumps(out)
        if s is prompts.TRIAGE_SCHEMA:
            batch = prompt.split("COMMENTS TO TRIAGE:", 1)[1]
            ids = re.findall(r"^\[([^\]]+)\]", batch, re.M)
            out = []
            for i, cid in enumerate(ids):
                rebut = i == 0
                out.append({
                    "comment_id": cid, "category": "methods",
                    "agreement": "disagree" if rebut else "agree", "agreement_confidence": 0.7,
                    "stance": "rebut" if rebut else "address", "already_addressed": "no",
                    "evidence": "", "required_changes": [] if rebut else ["Do the thing"],
                    "effort_level": "small", "hours_low": 1, "hours_high": 3 if rebut else 1.5, "needs_new_data": False,
                    "effort_confidence": 0.6, "rebuttal": "We respectfully disagree because X." if rebut else "",
                    "rebuttal_confidence": 0.55 if rebut else 0, "rationale": "Because.",
                })
            return json.dumps(out)
        if s is prompts.CROSS_COMMENT_SCHEMA:
            ids = re.findall(r"^\[([^\]]+)\]", prompt, re.M)
            return json.dumps({"conflicts": [{"comment_ids": ids[:2], "description": "Opposite asks",
                                              "suggested_resolution": "Pick one"}] if len(ids) > 1 else [],
                               "duplicates": [], "themes": [{"theme": "Statistics", "comment_ids": ids[:3]}],
                               "reviewers": [{"reviewer": "Reviewer 2", "expertise_guess": "statistician",
                                              "main_concerns": ["pseudo-replication"], "tone": "hostile",
                                              "how_to_respond": "Show the corrected model first."}]})
        if s is prompts.DEDUP_SCHEMA:
            return json.dumps({"groups": [[1, 2]]})
        if s is prompts.META_REVIEW_SCHEMA:
            return json.dumps({
                "summary": "Underpowered and overclaimed.", "strengths": ["Clear question"],
                "key_weaknesses": ["Pseudo-replication"], "recommendation": "major_revision",
                "recommendation_confidence": 0.8, "effort_level": "moderate", "effort_hours_low": 20,
                "effort_hours_high": 60, "effort_confidence": 0.55, "effort_rationale": "Reanalysis needed.",
                "top_priorities": [{"title": "Fix stats", "why": "Invalid", "item_ids": ["ai-001"]}],
                "rebuttal_strategy": "Concede most.", "likely_outcome": "Accept after major revision."})
        if s is prompts.GAP_ANALYSIS_SCHEMA:
            ids = re.findall(r"^\[([^\]]+)\]", prompt.split("COMMENTS TO ASSESS:", 1)[1], re.M)
            return json.dumps([{"comment_id": i, "status": "not_addressed", "evidence": "",
                                "suggested_action": "Do it"} for i in ids])
        if s is prompts.PATCH_PROPOSAL_SCHEMA:
            return json.dumps({"find": "Groups were compared with independent t-tests on all eyes.",
                               "replace": "Interocular differences were compared with t-tests.",
                               "rationale": "Avoid pseudo-replication."})
        if s is prompts.RESPONSE_LETTER_ITEM_SCHEMA:
            return json.dumps({"response_text": "We thank the reviewer and have revised the text."})
        if s is prompts.VENT_SCHEMA:
            return json.dumps({"response_text": "We thank the reviewer; the method is described in Section 2.4.",
                               "manuscript_change": "Add 'see Section 2.4' to line 38.",
                               "notes_for_author": "Dropped the sarcasm."})
        return "A thoughtful co-author reply."

    @staticmethod
    def _item(title, category, severity, quote, conf):
        return {"title": title, "category": category, "severity": severity, "location": "Results",
                "quote": quote, "issue": f"{title}: details.", "rationale": "It matters.",
                "suggestion": "Fix it.", "effort_level": "small", "hours_low": 1, "hours_high": 4,
                "needs_new_data": False, "effort_confidence": 0.6, "confidence": conf}


class TempProjects(unittest.TestCase):
    """Every test gets its own projects folder, config folder and scratch dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="papr-test-"))
        self._old_root = project_mod.PROJECTS_ROOT
        project_mod.PROJECTS_ROOT = self.tmp / "projects"
        self._old_cfg = os.environ.get("PAPR_CONFIG_DIR")
        os.environ["PAPR_CONFIG_DIR"] = str(self.tmp / "config")
        self.work = self.tmp / "work"
        self.work.mkdir()

    def tearDown(self):
        project_mod.PROJECTS_ROOT = self._old_root
        if self._old_cfg is None:
            os.environ.pop("PAPR_CONFIG_DIR", None)
        else:
            os.environ["PAPR_CONFIG_DIR"] = self._old_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_demo(self) -> Path:
        dest = self.work / "demo-paper"
        shutil.copytree(DEMO, dest)
        return dest

    def demo_project(self, with_reviews: bool = True, with_code: bool = True, with_pdf: bool = True):
        demo = self.copy_demo()
        kw = {}
        if with_code:
            kw = {"code_paths": [str(demo / "code")], "data_paths": [str(demo / "data")]}
        if with_pdf:
            kw["submitted_pdf"] = str(demo / "submitted.pdf")
        letter = demo / "reviews" / "decision-letter.txt"
        proj = project_mod.Project.create(
            "Demo", journal="IOVS",
            manuscript_files=[{"path": str(demo / "manuscript" / "main.tex"), "label": "main.tex"}],
            comment_sources=[{"path": str(letter), "label": "letter"}] if with_reviews else [], **kw)
        if with_reviews:
            items = review_letters.extract_comments(FakeLLM(), "letter", documents.extract_text(letter))
            review_letters.replace_source_comments(proj, "letter", items)
        return proj, demo
