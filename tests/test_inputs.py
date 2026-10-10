"""Reading what the author provides: documents, LaTeX, chunking, review
letters, code and data (.csv/.mat/.xlsx/notebooks), line references, folder
discovery."""
from __future__ import annotations

import json
import math
import struct
import unittest
import zipfile

from support import DEMO, HAS_PDFTOTEXT, FakeLLM, TempProjects

from papr_thinkr.inputs import chunking, code_and_data, documents, line_refs, matfile, review_letters
from papr_thinkr.inputs.discover import discover
from papr_thinkr.project import Project

MS = DEMO / "manuscript" / "main.tex"
LETTER = DEMO / "reviews" / "decision-letter.txt"


class TestDocuments(TempProjects):
    def test_roundtrip_preserves_encoding_newlines_bom(self):
        p = self.work / "m.tex"
        raw = "\ufeffCaf\u00e9 r\u00e9sum\u00e9\r\nline two\r\n".encode("utf-8")
        p.write_bytes(raw)
        text, meta = documents.read_text_file(p)
        self.assertEqual(text, "Caf\u00e9 r\u00e9sum\u00e9\nline two\n")
        self.assertTrue(meta.bom)
        self.assertEqual(meta.newline, "\r\n")
        documents.write_text_file(p, text, meta)
        self.assertEqual(p.read_bytes(), raw)

    def test_cp1252_file_written_back_as_cp1252(self):
        p = self.work / "old.tex"
        p.write_bytes("na\u00efve \u2013 test\n".encode("cp1252"))
        text, meta = documents.read_text_file(p)
        self.assertEqual(meta.encoding, "cp1252")
        documents.write_text_file(p, text.replace("test", "tested"), meta)
        self.assertEqual(p.read_bytes(), "na\u00efve \u2013 tested\n".encode("cp1252"))
        with self.assertRaises(documents.IngestError):
            documents.write_text_file(p, text + "\u4e2d", meta)

    def test_tex_flatten_body_and_chunks(self):
        (self.work / "sections").mkdir()
        (self.work / "sections" / "methods.tex").write_text("\\section{Methods}\nWe did X.\n", encoding="utf-8")
        main = self.work / "main.tex"
        main.write_text("\\documentclass{article}\n\\usepackage{foo}\n\\title{A \\emph{great} study}\n"
                        "\\begin{document}\n\\section{Intro}\nHello.\n\\input{sections/methods}\n"
                        "% \\input{sections/missing}\n\\end{document}\n", encoding="utf-8")
        body = documents.tex_body(documents.flatten_tex(main))
        self.assertIn("We did X.", body)
        self.assertNotIn("usepackage", body)
        self.assertIn("\\title{A \\emph{great} study}", body)
        self.assertEqual([p.name for p in documents.tex_component_files(main)], ["main.tex", "methods.tex"])
        self.assertEqual([c.heading for c in chunking.chunk_manuscript(body, ".tex") if c.kind == "body"],
                         ["Intro", "Methods"])

    def test_demo_chunks_and_retrieval(self):
        text = documents.manuscript_review_text(MS)
        chunks = chunking.chunk_manuscript(text, ".tex")
        heads = [c.heading for c in chunks]
        for h in ("Abstract", "Choroidal thickness", "Statistics", "Discussion", "References"):
            self.assertIn(h, heads)
        top = chunking.find_chunk_for_query(chunks, "Figure 3 choroid thickened", 1)[0]
        self.assertIn(top.heading, ("Results", "Choroidal thickness"))
        small, note = chunking.fit_text(text, chunks, 1200, focus_query="t-tests statistics eyes")
        self.assertLessEqual(len(small), 1250)
        self.assertIn("t-tests", small)
        self.assertIsNotNone(note)

    def test_plain_text_headings_and_split(self):
        plain = "Title\n\nAbstract\n\nWe did things.\n\n1. Introduction\n\nIntro text.\n\n2. Methods\n\nMethod text.\n"
        self.assertIn("Introduction", [c.heading for c in chunking.chunk_manuscript(plain, ".txt")])
        text = "\n\n".join(f"Paragraph {i} " + "word " * 50 for i in range(10))
        parts = chunking.split_text(text, 700)
        self.assertTrue(all(len(p) <= 700 and p.startswith("Paragraph") for p in parts))


class TestReviewLetters(TempProjects):
    def test_sections_ignore_sentences(self):
        labels = [lab for lab, _ in review_letters._split_into_sections(LETTER.read_text(encoding="utf-8"))]
        self.assertEqual(labels, ["Editor", "Reviewer 1", "Reviewer 2"])

    def test_ids_url_safe_and_ordered(self):
        items = review_letters.extract_comments(FakeLLM(), "Decision #1 / round 2.docx", LETTER.read_text(encoding="utf-8"))
        self.assertTrue(all(c["id"].replace(".", "").replace("-", "").isalnum() for c in items))
        r2 = [c for c in items if c["reviewer"] == "Reviewer 2"]
        self.assertEqual([c["order"] for c in r2], list(range(1, len(r2) + 1)))
        self.assertGreaterEqual(len(r2), 6)

    def test_regex_fallback(self):
        items = review_letters.extract_comments(None, "letter", LETTER.read_text(encoding="utf-8"), use_llm=False)
        self.assertTrue(any("power analysis" in c["comment_text"] for c in items))

    def test_reextract_preserves_user_work(self):
        demo = self.copy_demo()
        letter = demo / "reviews" / "decision-letter.txt"
        proj = Project.create("t", manuscript_files=[{"path": str(demo / "manuscript" / "main.tex"), "label": "m"}],
                              comment_sources=[{"path": str(letter), "label": "letter"}])
        text = letter.read_text(encoding="utf-8")
        review_letters.replace_source_comments(proj, "letter", review_letters.extract_comments(FakeLLM(), "letter", text))
        cid = proj.state["comments"][1]["id"]
        proj.update_comment(cid, author_notes="my note", status="addressed")
        review_letters.replace_source_comments(proj, "letter", review_letters.extract_comments(FakeLLM(), "letter", text))
        proj.reload()
        self.assertEqual(proj.get_comment(cid)["author_notes"], "my note")
        self.assertEqual(len({c["id"] for c in proj.state["comments"]}), len(proj.state["comments"]))


class TestCodeAndData(TempProjects):
    def test_demo_dossier_finds_the_planted_problems(self):
        d = code_and_data.build_dossier([str(DEMO / "code")], [str(DEMO / "data")], budget_chars=60_000)
        titles = " | ".join(f["title"] for f in d.findings)
        for needle in ("setwd()", "package versions not recorded (1 package)", "dropped silently",
                       "no multiple-comparison correction", "without set.seed", "duplicate row",
                       "Missing values (NaN) in data/choroid_oct.mat", "No README"):
            self.assertIn(needle, titles)
        self.assertNotIn("absolute paths", titles)          # the setwd() line is reported once, as setwd
        self.assertIn("thickness_um: double 24\u00d72", d.text)
        self.assertIn("contents {'blue': 12, 'control': 12}", d.text)
        self.assertIn(" 7| t.test(dR ~ group, data = d)", d.text)

    def test_missing_input_and_pii(self):
        code = self.work / "code"
        code.mkdir()
        (code / "run.R").write_text('d <- read.csv("inputs/cohort.csv")\nset.seed(1)\nsample(d)\n'
                                    'write.csv(d, "out/results.csv")\n', encoding="utf-8")
        data = self.work / "data"
        data.mkdir()
        (data / "people.csv").write_text("id,email,age\n1,a@b.c,30\n2,,41\n", encoding="utf-8")
        titles = " | ".join(f["title"] + " " + f["issue"] for f in code_and_data.build_dossier([str(code)], [str(data)]).findings)
        self.assertIn("cohort.csv", titles)
        self.assertNotIn("results.csv", titles)
        self.assertIn("personal data", titles)
        self.assertNotIn("set.seed", titles)

    def test_xlsx_and_notebook(self):
        p = self.work / "t.xlsx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<sheets><sheet name="Data" sheetId="1"/></sheets></workbook>')
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<si><t>dose</t></si><si><t>resp</t></si></sst>')
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                       '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>2.5</v></c></row>'
                       '<row r="3"><c r="A3"><v>2</v></c><c r="B3"><v>3.5</v></c></row></sheetData></worksheet>')
        s = code_and_data.summarize_xlsx(p)
        self.assertEqual((s["sheets"], s["n_rows"], s["columns"][1]["mean"]), (["Data"], 2, 3.0))
        nb = {"cells": [{"cell_type": "code", "execution_count": 2, "source": ["x = 1"]},
                        {"cell_type": "code", "execution_count": 1, "source": ["y = 2"]}]}
        (self.work / "nb").mkdir()
        (self.work / "nb" / "a.ipynb").write_text(json.dumps(nb), encoding="utf-8")
        self.assertTrue(any("top-to-bottom" in f["title"]
                            for f in code_and_data.build_dossier([str(self.work / "nb")], []).findings))


def _mat_v5(name: str, values: list[float], rows: int, cols: int) -> bytes:
    """A minimal uncompressed MAT v5 file with one double matrix (no SciPy needed)."""
    def element(mi_type, data):
        pad = (8 - len(data) % 8) % 8
        return struct.pack("<II", mi_type, len(data)) + data + b"\x00" * pad
    flags = element(6, struct.pack("<II", 6, 0))                      # mxDOUBLE_CLASS
    dims = element(5, struct.pack("<ii", rows, cols))
    nm = element(1, name.encode())
    pr = element(9, struct.pack(f"<{len(values)}d", *values))
    body = flags + dims + nm + pr
    header = b"MATLAB 5.0 MAT-file, made by a test".ljust(116, b" ") + b"\x00" * 8 + struct.pack("<H", 0x0100) + b"IM"
    return header + struct.pack("<II", 14, len(body)) + body


class TestMatFiles(TempProjects):
    def test_pure_python_reader(self):
        p = self.work / "x.mat"
        p.write_bytes(_mat_v5("thickness", [1.0, 2.0, float("nan"), 4.0, 5.0, 6.0], 3, 2))
        s = matfile.summarize_mat(p)
        v = s["variables"][0]
        self.assertEqual((s["format"], v["name"], v["class"], v["shape"]), ("MAT v5/v7", "thickness", "double", [3, 2]))
        self.assertEqual((v["stats"]["nan"], v["stats"]["mean"]), (1, 3.6))

    def test_demo_mat_matches_scipy(self):
        try:
            from scipy.io import loadmat
        except ImportError:
            self.skipTest("SciPy not installed")
        ours = {v["name"]: v for v in matfile.summarize_mat(DEMO / "data" / "choroid_oct.mat")["variables"]}
        ref = loadmat(str(DEMO / "data" / "choroid_oct.mat"))["thickness_um"]
        finite = [float(x) for x in ref.ravel() if not math.isnan(float(x))]
        self.assertAlmostEqual(ours["thickness_um"]["stats"]["mean"], sum(finite) / len(finite), places=2)
        self.assertEqual(ours["meta"]["fields"], ["device", "scan", "segmentation"])

    def test_compressed_v7(self):
        try:
            import numpy as np
            from scipy.io import savemat
        except ImportError:
            self.skipTest("SciPy not installed")
        p = self.work / "c.mat"
        savemat(str(p), {"a": np.arange(10.0), "s": {"f": "text"}}, do_compression=True)
        names = {v["name"]: v for v in matfile.summarize_mat(p)["variables"]}
        self.assertEqual(names["a"]["stats"]["mean"], 4.5)
        self.assertEqual(names["s"]["field_values"]["f"]["text"], ["text"])

    def test_v73_needs_h5py(self):
        p = self.work / "big.mat"
        p.write_bytes(b"MATLAB 7.3 MAT-file".ljust(512, b" ") + b"\x89HDF\r\n\x1a\n" + b"\x00" * 64)
        s = matfile.summarize_mat(p)
        self.assertEqual(s["format"], "MAT v7.3 (HDF5)")


class TestLineRefs(TempProjects):
    def test_every_fifth_line_and_section_numbers(self):
        lines = []
        for n in range(1, 31):
            num = f"{n} " if n % 5 == 0 else ""
            lines.append(f"{num}text of line {n} with sig-" if n == 11 else f"{num}text of line {n}")
        lines[13] = "2 14 Methods"          # a section number printed before the line number
        doc = line_refs._parse("\n".join(lines), "x.pdf")
        self.assertEqual(doc.lines[12], "text of line 12")
        refs = line_refs.resolve_refs("Lines 11-12: unclear. Also L 3.", doc)
        self.assertEqual(refs[0]["ref"], "lines 11\u201312")
        self.assertIn("text of line 3", refs[1]["text"])

    @unittest.skipUnless(HAS_PDFTOTEXT, "pdftotext not installed")
    def test_demo_submitted_pdf(self):
        doc = line_refs.parse_numbered_pdf(DEMO / "submitted.pdf")
        refs = line_refs.resolve_refs("Lines 38-39: no method for choroid", doc)
        self.assertIn("choroid thickened", refs[0]["text"])
        comments = [{"comment_text": "Line 6 and line 38: p = 0.000", "origin": "reviewer"}]
        self.assertEqual(line_refs.annotate_comments(comments, str(DEMO / "submitted.pdf")), 1)
        self.assertEqual([r["ref"] for r in comments[0]["resolved_refs"]], ["line 6", "line 38"])


class TestDiscover(TempProjects):
    def test_demo_folder(self):
        f = discover(DEMO)
        self.assertTrue(f["manuscript"].endswith("main.tex"))
        self.assertEqual([p.split("\\")[-1].split("/")[-1] for p in f["reviews"]], ["decision-letter.txt"])
        self.assertTrue(f["code_paths"][0].endswith("code"))
        self.assertTrue(f["data_paths"][0].endswith("data"))
        if HAS_PDFTOTEXT:
            self.assertTrue(f["submitted_pdf"].endswith("submitted.pdf"))

    def test_word_project_ignores_own_response_letter(self):
        root = self.work / "paper"
        (root / "analysis").mkdir(parents=True)
        for name in ("Smith_manuscript_v3.docx", "Reviewer comments R1.docx", "Response to reviewers.docx"):
            with zipfile.ZipFile(root / name, "w") as z:
                z.writestr("word/document.xml", "<x/>")
        (root / "analysis" / "fit.R").write_text("x <- 1\n", encoding="utf-8")
        (root / "analysis" / "raw.mat").write_bytes(b"MATLAB 5.0")
        f = discover(root)
        self.assertTrue(f["manuscript"].endswith("Smith_manuscript_v3.docx"))
        self.assertEqual(len(f["reviews"]), 1)
        self.assertTrue(f["reviews"][0].endswith("Reviewer comments R1.docx"))
        self.assertTrue(any("response" in n for n in f["notes"]))


if __name__ == "__main__":
    unittest.main()
