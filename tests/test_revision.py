"""After the review: edits to the manuscript, gap analysis, chat, vent-to-reply."""
from __future__ import annotations

import unittest
from pathlib import Path

from support import FakeLLM, TempProjects

from papr_thinkr.llm import LLMError
from papr_thinkr.project import Project
from papr_thinkr.revision import chat, gap_analysis, patches, response_letter


class TestPatches(TempProjects):
    def _proj(self, ms: Path) -> Project:
        return Project.create("p", manuscript_files=[{"path": str(ms), "label": ms.name}])

    def test_apply_preserves_crlf_and_encoding_and_reverts(self):
        ms = self.work / "paper.txt"
        original = "Intro caf\u00e9.\r\nGroups were compared with independent t-tests on all eyes.\r\nEnd.\r\n"
        ms.write_bytes(original.encode("cp1252"))
        proj = self._proj(ms)
        with proj.mutate():
            proj.state["comments"].append({"id": "c1", "comment_text": "pseudo-replication", "patch_ids": [],
                                           "reviewer": "R", "origin": "reviewer",
                                           "resolved_refs": [{"ref": "line 2", "page": 1, "text": "Groups were"}]})
        fake = FakeLLM()
        patch = patches.propose_patch(fake, proj, proj.get_comment("c1"))
        self.assertIn("refers to this text in the submitted PDF", fake.calls[-1]["prompt"])
        self.assertTrue(patch["validated"], patch["validation_message"])
        diff = patches.diff_preview(proj, patch)
        self.assertIn("-Groups were compared", diff)
        self.assertIn("+Interocular differences", diff)
        patches.apply_patch(proj, patch["id"])
        after = ms.read_bytes().decode("cp1252")
        self.assertIn("Interocular differences were compared with t-tests.\r\n", after)
        proj.reload()
        self.assertEqual(len(list(proj.backups_dir.iterdir())), 1)
        with self.assertRaises(patches.PatchError):
            patches.reject_patch(proj, patch["id"])
        patches.revert_patch(proj, patch["id"])
        self.assertEqual(ms.read_bytes(), original.encode("cp1252"))

    def test_diff_preview_mid_line(self):
        ms = self.work / "p.md"
        ms.write_text("alpha beta gamma\nsecond line\n", encoding="utf-8")
        proj = self._proj(ms)
        p = proj.add_patch(file=str(ms), find="beta", replace="BETA", rationale="", comment_id=None)
        diff = patches.diff_preview(proj, p)
        self.assertIn("-alpha beta gamma", diff)
        self.assertIn("+alpha BETA gamma", diff)

    def test_ambiguous_non_editable_and_included_tex(self):
        ms = self.work / "p.md"
        ms.write_text("same. same.\n", encoding="utf-8")
        self.assertIsNone(patches.locate(ms, "same.")[0])
        proj = Project.create("d", manuscript_files=[{"path": str(self.work / "x.docx"), "label": "x"}])
        patch = {"file": str(self.work / "x.docx"), "find": "a", "replace": "b"}
        patches._validate_patch(proj, patch)
        self.assertIn("by hand", patch["validation_message"])
        (self.work / "methods.tex").write_text("We used ANOVA here.\n", encoding="utf-8")
        main = self.work / "main.tex"
        main.write_text("\\begin{document}\n\\input{methods}\n\\end{document}\n", encoding="utf-8")
        self.assertEqual(patches.locate(main, "We used ANOVA here.")[0].name, "methods.tex")


class TestGapChatVent(TempProjects):
    def test_gap_analysis_maps_status_and_respects_wontfix(self):
        proj, _ = self.demo_project(with_reviews=True, with_code=False, with_pdf=False)
        first = proj.state["comments"][0]["id"]
        proj.update_comment(first, status="wontfix")
        gap_analysis.run_gap_analysis(FakeLLM(), proj)
        proj.reload()
        statuses = {c["id"]: c["status"] for c in proj.state["comments"]}
        self.assertEqual(statuses[first], "wontfix")
        self.assertTrue(all(s in ("unaddressed", "wontfix") for s in statuses.values()))

    def test_chat_does_not_persist_on_failure(self):
        proj, _ = self.demo_project(with_reviews=False, with_code=False, with_pdf=False)

        class Broken(FakeLLM):
            def _chat(self, *a):
                raise LLMError("down")
        with self.assertRaises(LLMError):
            chat.project_chat_reply(Broken(), proj, "main", "hello")
        proj.reload()
        self.assertEqual(proj.get_thread("main", create=False)["messages"], [])
        chat.project_chat_reply(FakeLLM(), proj, "main", "hello")
        proj.reload()
        self.assertEqual(len(proj.get_thread("main")["messages"]), 2)

    def test_vent_to_reply(self):
        proj, _ = self.demo_project(with_reviews=True, with_code=False, with_pdf=False)
        c = proj.state["comments"][1]
        fake = FakeLLM()
        out = response_letter.vent_to_reply(fake, proj, c, "They obviously didn't read the Methods!")
        self.assertIn("Section 2.4", out["response_text"])
        self.assertTrue(out["manuscript_change"] and out["notes_for_author"])
        self.assertIn("didn't read the Methods", fake.calls[-1]["prompt"])


if __name__ == "__main__":
    unittest.main()
