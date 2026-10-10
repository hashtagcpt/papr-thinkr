"""The reviewer end to end (with the fake LLM): first-draft critique,
revision triage with decoding, figures, deterministic checks, the report."""
from __future__ import annotations

import unittest
import zipfile

from support import DEMO, HAS_PDFTOTEXT, FakeLLM, TempProjects

from papr_thinkr import review
from papr_thinkr.report import review_report, word
from papr_thinkr.review import checks
from papr_thinkr.revision import response_letter


class TestManuscriptChecks(TempProjects):
    def test_demo_manuscript(self):
        path = DEMO / "manuscript" / "main.tex"
        text = path.read_text(encoding="utf-8")
        titles = " | ".join(f["title"] for f in checks.check_manuscript(text, str(path)))
        for needle in ("p = 0.000", "Overclaiming", "ethics", "effect sizes", "limitations"):
            self.assertIn(needle.lower(), titles.lower())
        self.assertNotIn("citation key", titles)            # both \\cite keys exist in refs.bib
        self.assertNotIn("undefined cross-reference", titles)

    def test_latex_refs_and_cites(self):
        (self.work / "refs.bib").write_text("@article{known2020, title={x}}\n", encoding="utf-8")
        main = self.work / "m.tex"
        text = ("\\begin{document}\nSee Fig.~\\ref{fig:a} and \\ref{fig:missing}. TODO fix.\n"
                "\\cite{known2020,ghost2021}\n\\begin{figure}\\label{fig:a}\\end{figure}\n"
                "\\begin{table}\\label{tab:unused}\\end{table}\n\\bibliography{refs}\n\\end{document}\n")
        main.write_text(text, encoding="utf-8")
        found = checks.check_manuscript(text, str(main))
        missing = next(f for f in found if "citation key" in f["title"])
        self.assertIn("ghost2021", missing["issue"])
        self.assertNotIn("known2020", missing["issue"])
        joined = " | ".join(f["title"] + f["issue"] for f in found)
        self.assertIn("fig:missing", joined)
        self.assertIn("tab:unused", joined)

    def test_figure_number_gap(self):
        self.assertTrue(any("Figure 2" in f["title"]
                            for f in checks._check_figure_numbers("See Figure 1 and Figure 3. Table 1.")))


class TestReviewPipeline(TempProjects):
    def test_first_draft_review(self):
        proj, _demo = self.demo_project(with_reviews=False)
        fake = FakeLLM(vision=True)
        r = review.run_review(fake, proj, depth="thorough")
        self.assertEqual(r["mode"], "first-draft")
        names = [p["name"] for p in r["passes"]]
        self.assertEqual(sum(n.startswith("Referee:") for n in names), 5)
        self.assertIn("Code & data audit", names)
        self.assertIn("Figure 2 (with image)", names)
        fig_call = next(c for c in fake.calls if "FIGURE referee" in c["prompt"] and "FIGURE 2 (label" in c["prompt"])
        self.assertTrue(fig_call["images"] and fig_call["images"][0].endswith("fig2_axial.png"))
        self.assertIn("make_figures.R line 23", fig_call["prompt"])
        self.assertIn("DATA READ BY THIS CODE", fig_call["prompt"])
        proj.reload()
        ai = [c for c in proj.state["comments"] if c["origin"] == "ai-review"]
        titles = [c["critique"]["title"] for c in ai]
        self.assertEqual(titles.count("Unblinded refraction measurement"), 1)
        self.assertIn("Figure 1: caption says SD but the code plots the standard error", titles)
        made_up = next(c for c in ai if c["critique"]["title"] == "Made-up problem")
        self.assertFalse(made_up["critique"]["quote_verified"])
        self.assertEqual(len(r["figures"]), 3)
        self.assertEqual(r["overall"]["recommendation"], "major_revision")

    def test_revision_review_decodes_and_triages(self):
        proj, _demo = self.demo_project(with_reviews=True)
        r = review.run_review(FakeLLM(max_parallel=3), proj, depth="standard")
        self.assertEqual(r["mode"], "revision")
        proj.reload()
        rev = [c for c in proj.state["comments"] if c["origin"] == "reviewer"]
        self.assertTrue(all(c.get("triage") and c.get("decoded") for c in rev))
        misread = [c for c in rev if c["decoded"]["misunderstanding"] != "none"]
        self.assertTrue(misread and misread[0]["decoded"]["evidence_verified"])
        self.assertTrue(any(c["decoded"]["clarity"] == "unclear" for c in rev))
        self.assertGreater(r["n_misread"], 0)
        self.assertEqual(r["cross_comment"]["reviewers"][0]["expertise_guess"], "statistician")
        fig2 = next(c for c in rev if c["comment_text"].startswith("Fig. 2"))
        self.assertEqual(fig2["figure_refs"], [2])
        if HAS_PDFTOTEXT:
            p_comment = next(c for c in rev if c["comment_text"].startswith("Line 6 and line 38"))
            self.assertEqual([x["ref"] for x in p_comment["resolved_refs"]], ["line 6", "line 38"])

    def test_rerun_keeps_notes_and_authors_stance(self):
        proj, _demo = self.demo_project(with_reviews=True, with_pdf=False)
        review.run_review(FakeLLM(), proj, depth="quick")
        proj.reload()
        ai = next(c for c in proj.state["comments"] if c["origin"] == "ai-review")
        rev = next(c for c in proj.state["comments"] if c["origin"] == "reviewer")
        proj.update_comment(ai["id"], author_notes="keep me")
        with proj.mutate():
            c = proj.get_comment(rev["id"])
            c["triage"]["stance"] = "clarify"
            c["triage"]["stance_set_by_author"] = True
        review.run_review(FakeLLM(), proj, depth="quick")
        proj.reload()
        self.assertEqual(proj.get_comment(ai["id"])["author_notes"], "keep me")
        self.assertEqual(proj.get_comment(rev["id"])["triage"]["stance"], "clarify")
        self.assertEqual(len({c["id"] for c in proj.state["comments"]}), len(proj.state["comments"]))

    def test_small_context_still_works(self):
        proj, _demo = self.demo_project(with_reviews=True, with_code=False, with_pdf=False)
        r = review.run_review(FakeLLM(context_chars=9000), proj, depth="standard")
        self.assertFalse([p for p in r["passes"] if not p["ok"]])


class TestReport(TempProjects):
    def test_report_sections_and_docx(self):
        proj, _demo = self.demo_project(with_reviews=True)
        review.run_review(FakeLLM(), proj, depth="quick")
        proj.reload()
        md = review_report.build_report_markdown(proj)
        for needle in ("At a glance", "Estimated revision effort", "Confidence in effort estimate", "Draft rebuttal",
                       "In plain words", "What would satisfy them", "Possible misreading", "Unclear comment",
                       "Who the reviewers are", "Figures and the code that draws them", "Quick wins",
                       "Independent critique", "Code and data provided", "code/make_figures.R:19"):
            self.assertIn(needle, md)
        if HAS_PDFTOTEXT:
            self.assertIn("(submitted PDF, p. 1) reads:", md)
        out = review_report.write_report(proj, self.work / "r.docx")
        self.assertTrue(zipfile.is_zipfile(out))
        self.assertTrue(out.with_suffix(".md").exists())
        fallback = self.work / "fallback.docx"
        word._simple_docx(md, fallback)
        with zipfile.ZipFile(fallback) as z:
            doc = z.read("word/document.xml").decode("utf-8")
        self.assertIn("At a glance", doc)
        self.assertIn("<w:tbl>", doc)
        self.assertTrue(zipfile.is_zipfile(response_letter.write_response_letter(proj, self.work / "letter.docx")))


if __name__ == "__main__":
    unittest.main()
