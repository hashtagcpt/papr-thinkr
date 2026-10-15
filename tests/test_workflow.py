"""Evidence, stale versions, resumable calls and submission exports."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from support import FakeLLM, TempProjects

from papr_thinkr import prompts
from papr_thinkr.llm import LLMCancelled, LLMError
from papr_thinkr.project import Project
from papr_thinkr.review import run_review
from papr_thinkr.revision import checklist, consistency, gap_analysis, patches, provenance, submission
from papr_thinkr.revision.cache import ReviewCache


QUOTE = "Participants were compared with paired tests."
RESPONSE = "We have revised the Methods to describe paired tests."


class AuditLLM(FakeLLM):
    def __init__(self, verdict="supported", evidence=QUOTE, unanswered=None):
        super().__init__()
        self.verdict, self.evidence, self.unanswered = verdict, evidence, unanswered or []

    def _chat(self, messages, temperature, json_schema, think):
        if json_schema is consistency.RESPONSE_CHECK_SCHEMA:
            self.calls.append({"schema": json_schema, "prompt": messages[-1]["content"]})
            path = re.search(r"^SOURCE (.+)$", messages[-1]["content"], re.M).group(1)
            return json.dumps({"summary": "Checked the response against the supplied sources.", "claims": [{
                "response_quote": RESPONSE, "verdict": self.verdict, "source_path": path,
                "evidence_quote": self.evidence, "reason": "Compare the named method with the current source."}],
                "unanswered_requests": self.unanswered})
        return super()._chat(messages, temperature, json_schema, think)


class WorkflowCase(TempProjects):
    def make_project(self, count=1):
        ms = self.work / "paper.md"
        ms.write_text("# Methods\n" + QUOTE + "\n# Results\nThe difference was small.\n", encoding="utf-8")
        proj = Project.create("Evidence", manuscript_files=[{"path": str(ms), "label": "paper"}])
        with proj.mutate():
            proj.state["comments"] = [{"id": f"c{i + 1}", "reviewer": "Reviewer 1", "origin": "reviewer",
                "comment_text": f"Describe the paired analysis, subrequest {i + 1}.", "status": "unaddressed",
                "response_text": "", "author_notes": "PRIVATE NOTE DO NOT EXPORT", "patch_ids": [],
                "triage": {"stance": "address", "required_changes": ["Describe the paired tests"]}}
                for i in range(count)]
        return proj, ms

    def complete(self, proj, ms):
        c = proj.get_comment("c1")
        aid = checklist.suggested_actions(c)[0]["id"]
        checklist.save_action(proj, "c1", {"done": True, "evidence": [{"path": str(ms), "quote": QUOTE}],
                                           "response_excerpt": RESPONSE}, aid)
        proj.update_comment("c1", status="addressed", response_text=RESPONSE)
        consistency.check_response(AuditLLM(), proj, "c1")
        return aid

    def test_complete_bundle_and_private_notes_excluded(self):
        proj, ms = self.make_project()
        self.complete(proj, ms)
        review = submission.build_submission_review(proj)
        self.assertTrue(review["ready"], review["issues"])
        bundle = submission.write_bundle(proj, self.work / "submission.zip")
        with zipfile.ZipFile(bundle) as archive:
            self.assertIn("response-letter.md", archive.namelist())
            self.assertIn("change-summary.md", archive.namelist())
            self.assertTrue(json.loads(archive.read("submission-review.json"))["ready"])
            self.assertIn("response-letter.docx", archive.namelist())
            all_text = "\n".join(archive.read(n).decode("utf-8") for n in archive.namelist() if not n.endswith(".docx"))
        self.assertNotIn("PRIVATE NOTE DO NOT EXPORT", all_text)
        self.assertNotIn("gap_plan_text", all_text)

    def test_snapshot_copies_manuscript_and_detects_all_source_roles(self):
        proj, ms = self.make_project()
        code = self.work / "analysis.py"
        data = self.work / "data.csv"
        code.write_text("print('analysis')", encoding="utf-8")
        data.write_text("x\n1\n2\n", encoding="utf-8")
        with proj.mutate():
            proj.config.update(code_paths=[str(code)], data_paths=[str(data)])
        snapshot = provenance.capture_snapshot(proj)
        self.assertEqual({r for f in snapshot["files"] for r in f["roles"]}, {"manuscript", "code", "data"})
        original = ms.read_bytes()
        ms.write_text("Changed outside the app", encoding="utf-8")
        data.write_text("x\n8\n", encoding="utf-8")
        view = provenance.revision_view(proj)
        self.assertEqual(set(view["changed_files"]), {str(ms.resolve()), str(data.resolve())})
        saved = proj.dir / "snapshots" / snapshot["id"] / snapshot["manuscript_copies"][0]["copy"]
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual(len(snapshot["manuscript_copies"]), 1)

    def test_stale_evidence_and_response_are_not_completion(self):
        proj, ms = self.make_project()
        aid = self.complete(proj, ms)
        ms.write_text(ms.read_text(encoding="utf-8") + "\nAn external edit.\n", encoding="utf-8")
        view = checklist.comment_view(proj, proj.get_comment("c1"))
        self.assertTrue(view["actions"][0]["evidence"][0]["verified"])
        self.assertFalse(view["complete"])
        self.assertEqual(consistency.check_view(proj.get_comment("c1"), provenance.source_manifest(proj))["freshness"], "stale")
        checklist.save_action(proj, "c1", {"evidence": [{"path": str(ms), "quote": QUOTE}]}, aid)
        self.assertTrue(checklist.comment_view(proj, proj.get_comment("c1"))["complete"])
        self.assertFalse(submission.build_submission_review(proj)["ready"])
        consistency.check_response(AuditLLM(), proj, "c1")
        self.assertTrue(submission.build_submission_review(proj)["ready"])

    def test_response_edits_invalidate_audit_and_paragraph_link(self):
        proj, ms = self.make_project()
        self.complete(proj, ms)
        proj.update_comment("c1", response_text="We will do this later. [insert page]")
        review = submission.build_submission_review(proj)
        codes = {i["code"] for i in review["issues"]}
        self.assertTrue({"response_audit_needed", "broken_response_link", "response_placeholder"} <= codes)

    def test_false_evidence_and_unrelated_patch_links_rejected(self):
        proj, ms = self.make_project()
        aid = checklist.suggested_actions(proj.get_comment("c1"))[0]["id"]
        for evidence in ([{"path": str(ms), "quote": "invented"}], [{"path": str(self.work / "secret"), "quote": "x"}]):
            with self.assertRaises(ValueError):
                checklist.save_action(proj, "c1", {"evidence": evidence}, aid)
        with self.assertRaises(ValueError):
            checklist.save_action(proj, "c1", {"patch_ids": ["another-comment-patch"]}, aid)

    def test_reasoned_rebuttal_does_not_require_fabricated_edit(self):
        proj, _ms = self.make_project()
        aid = checklist.suggested_actions(proj.get_comment("c1"))[0]["id"]
        checklist.save_action(proj, "c1", {"done": True, "rationale": "The existing paired design already accounts for this."}, aid)
        self.assertFalse(checklist.comment_view(proj, proj.get_comment("c1"))["complete"])
        proj.update_comment("c1", status="wontfix")
        self.assertTrue(checklist.comment_view(proj, proj.get_comment("c1"))["complete"])

    def test_reverted_edit_cannot_support_completion(self):
        proj, ms = self.make_project()
        p = proj.add_patch(file=str(ms), find="paired tests", replace="paired t-tests", rationale="Specify test", comment_id="c1")
        patches.apply_patch(proj, p["id"])
        aid = checklist.suggested_actions(proj.get_comment("c1"))[0]["id"]
        checklist.save_action(proj, "c1", {"done": True, "patch_ids": [p["id"]]}, aid)
        self.assertTrue(checklist.comment_view(proj, proj.get_comment("c1"))["complete"])
        patches.revert_patch(proj, p["id"])
        self.assertFalse(checklist.comment_view(proj, proj.get_comment("c1"))["complete"])
        self.assertIn("patch_not_current", {i["code"] for i in submission.build_submission_review(proj)["issues"]})

    def test_audit_cannot_validate_hallucinated_evidence(self):
        proj, _ms = self.make_project()
        proj.update_comment("c1", response_text=RESPONSE)
        result = consistency.check_response(AuditLLM(evidence="Made up evidence."), proj, "c1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["claims"][0]["verdict"], "unclear")
        self.assertIn("response_inconsistent", {i["code"] for i in submission.build_submission_review(proj)["issues"]})

    def test_audit_finds_unanswered_subrequests(self):
        proj, _ms = self.make_project()
        proj.update_comment("c1", response_text=RESPONSE)
        result = consistency.check_response(AuditLLM(unanswered=["Please report effect sizes as well."]), proj, "c1")
        self.assertFalse(result["ok"])
        self.assertTrue(result["unanswered_requests"])

    def test_review_cache_reuses_inputs_and_invalidates_affected_scopes(self):
        proj, _ms = self.make_project()
        data = self.work / "data.csv"
        data.write_text("x\n1\n", encoding="utf-8")
        with proj.mutate():
            proj.config["data_paths"] = [str(data)]
        fake = FakeLLM()
        messages = [{"role": "user", "content": "Summarize"}]
        cache = ReviewCache(proj, fake, provenance.source_manifest(proj))
        cache.chat_json(messages, roles=["manuscript"], json_schema=prompts.META_REVIEW_SCHEMA)
        cache.chat_json(messages, roles=["data"], json_schema=prompts.META_REVIEW_SCHEMA)
        self.assertEqual(len(fake.calls), 2)
        data.write_text("x\n9\n", encoding="utf-8")
        cache = ReviewCache(proj, fake, provenance.source_manifest(proj))
        cache.chat_json(messages, roles=["manuscript"], json_schema=prompts.META_REVIEW_SCHEMA)
        cache.chat_json(messages, roles=["data"], json_schema=prompts.META_REVIEW_SCHEMA)
        self.assertEqual((cache.hits, cache.misses, len(fake.calls)), (1, 1, 3))
        fake.model = "another-model"
        ReviewCache(proj, fake, provenance.source_manifest(proj)).chat_json(messages, roles=["manuscript"], json_schema=prompts.META_REVIEW_SCHEMA)
        self.assertEqual(len(fake.calls), 4)

    def test_failed_and_incomplete_calls_are_not_checkpoints(self):
        proj, _ms = self.make_project()
        class Broken(FakeLLM):
            def _chat(self, *args):
                raise LLMError("offline")
        cache = ReviewCache(proj, Broken(), provenance.source_manifest(proj))
        with self.assertRaises(LLMError):
            cache.chat_json([], json_schema=prompts.META_REVIEW_SCHEMA)
        self.assertFalse(list((proj.dir / "review-cache").glob("*.json")))
        cache = ReviewCache(proj, FakeLLM(), provenance.source_manifest(proj))
        cache.chat_json([{"role": "user", "content": "Summarize"}], json_schema=prompts.META_REVIEW_SCHEMA, cache_if=lambda r: False)
        self.assertFalse(list((proj.dir / "review-cache").glob("*.json")))

    def test_gap_analysis_resumes_after_cancellation_without_losing_batch(self):
        proj, _ms = self.make_project(count=8)
        class CancelSecond(FakeLLM):
            def _chat(self, *args):
                if len(self.calls) == 1:
                    raise LLMCancelled()
                return super()._chat(*args)
        with self.assertRaises(LLMCancelled):
            gap_analysis.run_gap_analysis(CancelSecond(), proj)
        proj.reload()
        self.assertEqual(sum(bool(c.get("gap_snapshot_id")) for c in proj.state["comments"]), 6)
        fake = FakeLLM()
        gap_analysis.run_gap_analysis(fake, proj)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(proj.state["gap_analysis_cache"]["reused_calls"], 1)
        self.assertTrue(all(c.get("gap_snapshot_id") for c in proj.state["comments"]))

    def test_selected_gap_analysis_leaves_other_comments_untouched(self):
        proj, _ms = self.make_project(count=2)
        gap_analysis.run_gap_analysis(FakeLLM(), proj, comment_ids=["c2"])
        self.assertNotIn("gap_snapshot_id", proj.get_comment("c1"))
        self.assertTrue(proj.get_comment("c2")["gap_snapshot_id"])
        self.assertIsNone(proj.state["gap_analysis_snapshot_id"])

    def test_full_review_can_resume_and_force_refresh(self):
        proj, _demo = self.demo_project(with_reviews=False, with_code=False, with_pdf=False)
        class InterruptAfterOne(FakeLLM):
            def _chat(self, *args):
                if len(self.calls) == 1:
                    raise LLMCancelled()
                return super()._chat(*args)
        with self.assertRaises(LLMCancelled):
            run_review(InterruptAfterOne(), proj, depth="quick")
        fake = FakeLLM()
        result = run_review(fake, proj, depth="quick")
        self.assertGreater(result["cache"]["reused_calls"], 0)
        fake = FakeLLM()
        result = run_review(fake, proj, depth="quick")
        self.assertEqual(len(fake.calls), 0)
        self.assertGreater(result["cache"]["reused_calls"], 0)
        run_review(fake, proj, depth="quick", force=True)
        self.assertGreater(len(fake.calls), 0)

    def test_reextract_preserves_checklist_and_audit(self):
        from papr_thinkr.inputs.review_letters import merge_preserving_user_work
        proj, ms = self.make_project()
        self.complete(proj, ms)
        old = proj.get_comment("c1")
        new = {"id": "new-id", "comment_text": old["comment_text"]}
        merged = merge_preserving_user_work([old], [new])[0]
        self.assertEqual(merged["revision_actions"], old["revision_actions"])
        self.assertEqual(merged["response_check"], old["response_check"])

    def test_unresolved_bundle_is_clearly_a_draft(self):
        proj, _ms = self.make_project()
        bundle = submission.write_bundle(proj)
        with zipfile.ZipFile(bundle) as archive:
            self.assertIn(b"DRAFT", archive.read("README.txt"))
            self.assertFalse(json.loads(archive.read("submission-review.json"))["ready"])

    def test_submission_cli_exit_code_and_export(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io
        from papr_thinkr.cli import main
        proj, ms = self.make_project()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["submission", proj.slug]), 2)
            self.complete(proj, ms)
            out = self.work / "cli-bundle.zip"
            self.assertEqual(main(["submission", proj.slug, "--out", str(out)]), 0)
            self.assertTrue(zipfile.is_zipfile(out))
