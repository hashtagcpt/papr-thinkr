"""Cross-section science audits, paired reviewer plans and LaTeX sources."""
from __future__ import annotations

import json
import re
from pathlib import Path

from support import FakeLLM, TempProjects

from papr_thinkr.inputs.discover import discover
from papr_thinkr.inputs.documents import read_manuscript, tex_component_files
from papr_thinkr.project import Project
from papr_thinkr.report.review_report import build_report_markdown
from papr_thinkr.review import run_review
from papr_thinkr.review.joint_plan import JOINT_PLAN_SCHEMA
from papr_thinkr.review.scientific import SCIENTIFIC_SCHEMA, ScientificReview
from papr_thinkr.revision import checklist, patches, provenance, response_letter


INTRO = "We ask whether paired measurements change over time."
METHOD = "A paired t-test was used for within-participant changes."
RESULT = "Paired differences improved by 2 units (p=0.04)."
DISCUSSION = "The treatment proves causation in all populations."
CODE = "stats.ttest_ind(before, after)"
STANDARD = "For repeated measurements, account for within-participant dependence."


class ScientificLLM(FakeLLM):
    def __init__(self, paths):
        super().__init__()
        self.paths = paths

    def evidence(self, name, quote, role):
        return {"source_path": str(self.paths[name].resolve()), "quote": quote, "role": role}

    def _chat(self, messages, temperature, json_schema, think):
        prompt = messages[-1]["content"]
        if json_schema is SCIENTIFIC_SCHEMA:
            self.calls.append({"schema": json_schema, "prompt": prompt})
            scope = re.search(r"AUDIT SCOPE: (\w+)", prompt).group(1)
            result = self.evidence("results", RESULT, "result")
            method = self.evidence("methods", METHOD, "statistics")
            standard = self.evidence("standards", STANDARD, "standard")
            link = {"label": "Paired change", "source_evidence": [result], "target_evidence": [method],
                "verdict": "questionable", "explanation": "The script uses an independent test despite paired observations.",
                "recommended_action": "Align the analysis with the paired design and report uncertainty.",
                "reported_analysis": "Paired t-test in text; independent t-test in code", "independent_unit": "participant",
                "assumptions": ["Check the distribution of paired differences"],
                "norm_basis": "supplied_standard", "norm_evidence": [standard]}
            findings = []
            if scope == "results_statistics":
                link["target_evidence"].append(self.evidence("code", CODE, "code"))
                for title, action in (("Analysis does not preserve pairing", "Use the design-appropriate paired analysis"),
                                      ("Uncertainty for paired change is missing", "Report the paired effect with a confidence interval")):
                    finding = self._item(title, "statistics", "major", RESULT, 0.9)
                    finding.update(issue=title, issue_kind="problem", actions=[{"action": action, "validation": "Compare the report and script against the paired design"}],
                        evidence=[result, method], norm_basis="supplied_standard", norm_evidence=[standard])
                    findings.append(finding)
            elif scope == "introduction_discussion":
                link.update(label="Question versus conclusion", source_evidence=[self.evidence("intro", INTRO, "introduction")],
                    target_evidence=[self.evidence("discussion", DISCUSSION, "discussion")], verdict="overclaim",
                    explanation="The discussion makes a causal and population-wide claim beyond the introductory question.",
                    recommended_action="Restore the stated scope and discuss alternative explanations.",
                    reported_analysis="", norm_basis="not_applicable", norm_evidence=[])
            elif scope == "missing_methods":
                link.update(label="Allocation reporting", verdict="not_reported", reported_analysis="",
                            explanation="The allocation procedure is not reported in the supplied methods.",
                            recommended_action="Describe how allocation was performed, without inventing a procedure.")
            else:
                link = None
                finding = self._item("Sensitivity analysis could strengthen interpretation", "interpretation", "minor", RESULT, 0.7)
                finding.update(issue_kind="strengthening", actions=[{"action": "Assess robustness to plausible exclusions", "validation": "Report whether the effect direction and interval change"}],
                               evidence=[result], norm_basis="model_inference", norm_evidence=[])
                findings.append(finding)
            return json.dumps({"summary": "Specific independent assessment: " + scope, "findings": findings,
                               "links": [link] if link else [], "coverage_note": "Source availability was checked."})
        if json_schema is JOINT_PLAN_SCHEMA:
            self.calls.append({"schema": json_schema, "prompt": prompt})
            ids = re.findall(r"^\[(ai-[^\]]+)\]", prompt.split("INDEPENDENT FINDINGS:\n", 1)[1], re.M)
            return json.dumps({"summary": "Keep the key rationale concise and move implementation detail to a supplement.",
                "reviewer_coverage": [{"finding_id": fid, "comment_ids": [], "relationship": "not_raised", "explanation": "The supplied requests concern length and detail, rather than this scientific issue."} for fid in ids], "actions": [{
                "title": "Concise rationale plus reproducible detail", "comment_ids": ["c1", "c2", "invented"],
                "related_finding_ids": ["ai-001", "made-up"], "approach": "Keep a brief rationale in the main text and add full detail to a supplement.",
                "manuscript_changes": ["Shorten the main paragraph", "Add implementation details to the supplement"],
                "acceptance_criteria": ["Both concerns have a specific manuscript location"], "compatibility": "compromise",
                "tradeoff": "Main-text brevity is preserved while detail remains available.",
                "responses": [{"comment_id": "c1", "response_outline": "Explain how the main text will be shortened."},
                              {"comment_id": "c2", "response_outline": "Point to the planned full methodological detail."}]}]})
        return super()._chat(messages, temperature, json_schema, think)


class TestScientificReview(TempProjects):
    def fixture(self, reviews=False, standards=True):
        parts = self.work / "sections"
        parts.mkdir()
        paths = {name: parts / f"{name}.tex" for name in ("intro", "methods", "results", "discussion")}
        paths["intro"].write_text("\\section{Introduction}\n" + INTRO + "\n" + "Background context. " * 80, encoding="utf-8")
        paths["methods"].write_text("\\section{Materials and methods}\nPaired repeated measurements were collected.\n\\subsection{Statistics}\n" + METHOD, encoding="utf-8")
        paths["results"].write_text("\\section{Results}\n" + RESULT, encoding="utf-8")
        paths["discussion"].write_text("\\section{Discussion}\n" + DISCUSSION, encoding="utf-8")
        main = self.work / "main.tex"
        main.write_text("\\documentclass{article}\n\\begin{document}\n\\input sections/intro\n\\input{sections/methods}\n"
                        "\\input{sections/results}\n\\input{sections/discussion}\n\\end{document}\n", encoding="utf-8")
        paths["main"] = main
        paths["code"] = self.work / "analysis.py"
        paths["code"].write_text("from scipy import stats\n" + CODE, encoding="utf-8")
        paths["standards"] = self.work / "field-standard.md"
        paths["standards"].write_text(STANDARD, encoding="utf-8")
        proj = Project.create("Scientific", field="Experimental psychology", manuscript_files=[{"path": str(main), "label": "main"}],
            code_paths=[str(paths["code"])], standards_paths=[str(paths["standards"])] if standards else [])
        if reviews:
            with proj.mutate():
                proj.state["comments"] = [{"id": f"c{i + 1}", "origin": "reviewer", "source": "letter", "reviewer": f"Reviewer {i + 1}",
                    "comment_text": text, "status": "unaddressed", "patch_ids": []} for i, text in enumerate(
                    ("Shorten the methods explanation.", "Explain the implementation in more detail."))]
        return proj, paths

    def test_latex_master_discovery_and_real_component_sources(self):
        proj, paths = self.fixture()
        found = discover(self.work)
        self.assertEqual(Path(found["manuscript"]), paths["main"])
        text = read_manuscript(paths["main"])
        for quote in (INTRO, METHOD, RESULT, DISCUSSION):
            self.assertIn(quote, text)
        self.assertEqual(len(tex_component_files(paths["main"])), 5)
        science = ScientificReview(proj, 100_000)
        self.assertEqual({s.role for s in science.sources if s.role != "other"}, {"introduction", "methods", "statistics", "result", "discussion", "standard"})
        self.assertIn(str(paths["results"].resolve()), science.documents)
        self.assertFalse([f for f in provenance.source_manifest(proj)["files"] if f.get("error")])

    def test_result_stats_map_preserves_distinct_problems_and_sources(self):
        proj, paths = self.fixture()
        llm = ScientificLLM(paths)
        review = run_review(llm, proj, depth="standard")
        audits = review["scientific_review"]["audits"]
        self.assertEqual({a["scope"] for a in audits}, {"introduction_discussion", "missing_methods", "results_statistics", "strengthening"})
        stats = next(a for a in audits if a["scope"] == "results_statistics")
        link = stats["links"][0]
        self.assertEqual(link["reported_analysis"], "Paired t-test in text; independent t-test in code")
        self.assertEqual(link["norm_basis"], "supplied_standard")
        self.assertTrue(link["verified"])
        self.assertTrue(all(e["verified"] for e in link["target_evidence"] + link["norm_evidence"]))
        findings = [c["critique"] for c in proj.state["comments"] if (c.get("critique") or {}).get("scope") == "results_statistics"]
        self.assertEqual(len(findings), 2)
        self.assertEqual(len({f["actions"][0]["action"] for f in findings}), 2)
        self.assertEqual(len(stats["finding_ids"]), 2)
        source_call = next(c for c in llm.calls if "AUDIT SCOPE: results_statistics" in c["prompt"])
        self.assertIn(CODE, source_call["prompt"])
        self.assertIn("independent experimental/sampling unit", source_call["prompt"])
        c = next(c for c in proj.state["comments"] if c.get("critique") in findings)
        self.assertTrue(checklist.suggested_actions(c)[0]["validation"])

    def test_intro_discussion_methods_and_uncaught_strengthening_in_report(self):
        proj, paths = self.fixture()
        review = run_review(ScientificLLM(paths), proj, depth="quick")
        audits = review["scientific_review"]["audits"]
        narrative = next(a for a in audits if a["scope"] == "introduction_discussion")
        self.assertEqual(narrative["links"][0]["verdict"], "overclaim")
        self.assertEqual(narrative["links"][0]["source_evidence"][0]["source_path"], str(paths["intro"].resolve()))
        self.assertTrue(any((c.get("critique") or {}).get("issue_kind") == "strengthening" for c in proj.state["comments"]))
        md = build_report_markdown(proj)
        for phrase in ("Independent scientific audit", "Introduction versus discussion", "Missing methods and reporting",
                       "Results linked to statistical analyses", "Independent unit", "paired", "Validation:"):
            self.assertIn(phrase, md)

    def test_field_norm_without_supplied_evidence_is_not_certified(self):
        proj, paths = self.fixture(standards=False)
        review = run_review(ScientificLLM(paths), proj, depth="quick")
        stats = next(a for a in review["scientific_review"]["audits"] if a["scope"] == "results_statistics")
        self.assertEqual(stats["links"][0]["norm_basis"], "model_inference")
        self.assertFalse(stats["links"][0]["norm_evidence"][0]["verified"])

    def test_independent_passes_do_not_receive_reviewer_requests(self):
        proj, paths = self.fixture(reviews=True)
        fake = ScientificLLM(paths)
        review = run_review(fake, proj, depth="quick")
        science_calls = [c for c in fake.calls if c["schema"] is SCIENTIFIC_SCHEMA]
        self.assertEqual(len(science_calls), 4)
        self.assertTrue(all("Shorten the methods explanation." not in c["prompt"] for c in science_calls))
        plan = review["joint_revision_plan"]
        self.assertEqual(plan["actions"][0]["comment_ids"], ["c1", "c2"])
        self.assertNotIn("made-up", plan["actions"][0]["related_finding_ids"])
        self.assertEqual(plan["uncovered_comment_ids"], [])
        self.assertTrue(all(proj.get_comment(cid)["joint_guidance"] for cid in ("c1", "c2")))
        self.assertIn("Joint revision plan across reviewers", build_report_markdown(proj))
        self.assertTrue(any((c.get("critique") or {}).get("reviewer_coverage", {}).get("relationship") == "not_raised"
                            for c in proj.state["comments"] if c["origin"] == "ai-review"))

    def test_shared_patch_links_both_comments_and_keeps_completion_explicit(self):
        proj, paths = self.fixture(reviews=True)
        run_review(ScientificLLM(paths), proj, depth="quick")
        class PatchLLM(ScientificLLM):
            def _chat(self, messages, temperature, json_schema, think):
                from papr_thinkr import prompts
                if json_schema is prompts.PATCH_PROPOSAL_SCHEMA:
                    self.calls.append({"schema": json_schema, "prompt": messages[-1]["content"]})
                    return json.dumps({"find": METHOD, "replace": METHOD + " Further details are in the supplement.", "rationale": "Answer both requests coherently."})
                return super()._chat(messages, temperature, json_schema, think)
        llm = PatchLLM(paths)
        patch = patches.propose_patch(llm, proj, proj.get_comment("c1"))
        self.assertEqual(patch["related_comment_ids"], ["c1", "c2"])
        self.assertEqual(Path(patch["file"]), paths["methods"])
        self.assertIn("Consider these reviewers together", llm.calls[-1]["prompt"])
        self.assertTrue(all(patch["id"] in proj.get_comment(cid)["patch_ids"] for cid in ("c1", "c2")))
        self.assertEqual(proj.get_comment("c2")["status"], "unaddressed")
        response_letter.draft_response_letter_item(llm, proj, proj.get_comment("c2"))
        self.assertIn("Planned changes (not evidence of completion)", llm.calls[-1]["prompt"])

    def test_long_results_receive_multiple_target_windows(self):
        proj, paths = self.fixture()
        paths["results"].write_text("\\section{Results}\n" + (RESULT + "\n\n") * 250, encoding="utf-8")
        science = ScientificReview(proj, 20_000)
        tasks = science.tasks()
        stats = [t for t in tasks if t["scope"] == "results_statistics"]
        self.assertGreater(len(stats), 1)
        self.assertTrue(all("RESULT TARGET" in t["messages"][-1]["content"] for t in stats))
        self.assertTrue(all(t["source_chars"] < 20_000 for t in tasks))

    def test_missing_or_truncated_methods_downgrades_absence_claim(self):
        proj, paths = self.fixture()
        paths["methods"].write_text("\\section{Unclassified supporting text}\n" + "A long description. " * 1000, encoding="utf-8")
        review = run_review(ScientificLLM(paths), proj, depth="quick")
        method = next(a for a in review["scientific_review"]["audits"] if a["scope"] == "missing_methods")
        self.assertEqual(method["links"][0]["verdict"], "insufficient_information")

    def test_component_include_can_resolve_from_master_root(self):
        proj, paths = self.fixture()
        extra = self.work / "shared.tex"
        extra.write_text("A root-level shared protocol.", encoding="utf-8")
        with paths["methods"].open("a", encoding="utf-8") as out:
            out.write("\n\\input{shared}\n")
        self.assertIn("A root-level shared protocol.", read_manuscript(paths["main"]))
        self.assertFalse([f for f in provenance.source_manifest(proj)["files"] if f.get("error")])

    def test_invented_result_quote_cannot_validate_statistical_link(self):
        proj, paths = self.fixture()
        class InventedQuote(ScientificLLM):
            def _chat(self, messages, temperature, json_schema, think):
                raw = super()._chat(messages, temperature, json_schema, think)
                if json_schema is SCIENTIFIC_SCHEMA and "AUDIT SCOPE: results_statistics" in messages[-1]["content"]:
                    result = json.loads(raw)
                    result["links"][0]["source_evidence"][0]["quote"] = "This result is not in the manuscript."
                    return json.dumps(result)
                return raw
        result = run_review(InventedQuote(paths), proj, depth="quick")
        link = next(a for a in result["scientific_review"]["audits"] if a["scope"] == "results_statistics")["links"][0]
        self.assertFalse(link["verified"])
        self.assertEqual(link["verdict"], "insufficient_information")

    def test_malformed_audit_is_reported_and_retried(self):
        proj, paths = self.fixture()
        class Malformed(ScientificLLM):
            def _chat(self, messages, temperature, json_schema, think):
                if json_schema is SCIENTIFIC_SCHEMA:
                    self.calls.append({"schema": json_schema, "prompt": messages[-1]["content"]})
                    return json.dumps({"findings": [{"evidence": "invalid shape"}], "links": []})
                return super()._chat(messages, temperature, json_schema, think)
        first = run_review(Malformed(paths), proj, depth="quick")
        self.assertTrue(all(a["error"] for a in first["scientific_review"]["audits"]))
        second_llm = ScientificLLM(paths)
        second = run_review(second_llm, proj, depth="quick")
        self.assertFalse(any(a["error"] for a in second["scientific_review"]["audits"]))
        self.assertEqual(sum(c["schema"] is SCIENTIFIC_SCHEMA for c in second_llm.calls), 4)

    def test_excerpted_review_does_not_certify_a_finding_was_uncaught(self):
        proj, paths = self.fixture(reviews=True)
        proj.update_comment("c1", comment_text="Please address these concerns. " * 300)
        result = run_review(ScientificLLM(paths), proj, depth="quick")
        self.assertTrue(result["joint_revision_plan"]["reviewer_coverage"])
        self.assertTrue(all(r["relationship"] == "uncertain" for r in result["joint_revision_plan"]["reviewer_coverage"]))

    def test_changing_standards_refreshes_scientific_audits(self):
        from papr_thinkr import prompts
        proj, paths = self.fixture()
        run_review(ScientificLLM(paths), proj, depth="quick")
        with paths["standards"].open("a", encoding="utf-8") as out:
            out.write("\nAlso report the effect estimate and interval.")
        fake = ScientificLLM(paths)
        run_review(fake, proj, depth="quick")
        self.assertEqual(sum(c["schema"] is SCIENTIFIC_SCHEMA for c in fake.calls), 4)
        general_calls = [c for c in fake.calls if c["schema"] is prompts.CRITIQUE_SCHEMA and "Review the whole manuscript" in c["prompt"]]
        self.assertEqual(general_calls, [])
