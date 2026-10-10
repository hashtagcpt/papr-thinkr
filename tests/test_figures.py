"""Figures: finding them in .tex/.docx/.md, mapping them to the R/Python/
MATLAB code that draws them, preparing images, and the caption-vs-code checks."""
from __future__ import annotations

import base64
import unittest
import zipfile
from pathlib import Path

from support import DEMO, HAS_PDFTOPPM, TempProjects

from papr_thinkr.inputs.documents import manuscript_review_text
from papr_thinkr.inputs.figures import (collect_figures, figure_outputs, image_size, read_code_texts)
from papr_thinkr.review.checks import check_figures

PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class TestDemoFigures(TempProjects):
    def setUp(self):
        super().setUp()
        ms = DEMO / "manuscript" / "main.tex"
        self.figs, self.unused = collect_figures(ms, read_code_texts([str(DEMO / "code")]),
                                                 cache_dir=self.work / "cache", manuscript_text=manuscript_review_text(ms))

    def test_figures_captions_and_mentions(self):
        self.assertEqual([f.number for f in self.figs], [1, 2, 3])
        self.assertIn("error bars show SD", self.figs[0].caption)
        self.assertEqual(self.figs[0].label, "fig:refraction")
        self.assertTrue(any("Fig. 1" in m for m in self.figs[0].mentions))
        self.assertTrue(any("35 µm" in m for m in self.figs[2].mentions))
        self.assertEqual(self.figs[2].missing, ["figures/fig3_choroid.png"])

    def test_code_mapping_r_and_matlab(self):
        f1, f2, f3 = self.figs
        self.assertEqual((f1.code[0]["file"], f1.code[0]["line"]), ("code/make_figures.R", 19))
        self.assertIn("lens <- subset(d, eye == \"lens\")", f1.code[0]["segment"])     # setup included
        self.assertNotIn("boxplot", f1.code[0]["segment"])                             # but not figure 2
        self.assertIn("dev.off()", f2.code[0]["segment"])
        self.assertNotIn("geom_errorbar", f2.code[0]["segment"])
        self.assertEqual(f3.code[0]["file"], "code/choroid_thickness.m")
        self.assertEqual(f3.code[0]["reads"], ["choroid_oct.mat"])
        self.assertEqual(self.unused, [])

    def test_images(self):
        self.assertEqual(self.figs[1].pixel_size, (900, 700))
        self.assertTrue(self.figs[1].image.endswith("fig2_axial.png"))
        if HAS_PDFTOPPM:
            self.assertTrue(Path(self.figs[0].image).is_file())        # PDF figure rendered to PNG
        self.assertIsNone(self.figs[2].image)

    def test_caption_vs_code_checks(self):
        found = {f["title"]: f for f in check_figures(self.figs, code_provided=True)}
        self.assertEqual(found["Figure 1: caption says SD but the code plots the standard error"]["severity"], "major")
        for t in ("Figure 2: check the n in the caption", "Figure 2: red/green colours",
                  "Figure 3: image file not found", "Figure 3: caption too short", "Figure 1: bar chart hides the data"):
            self.assertIn(t, found)
        self.assertNotIn("Figure 2: error bars not defined", found)        # boxplot, no error bars
        self.assertNotIn("Figure 1: check the n in the caption", found)    # subset(eye == "lens") isn't dropping data


class TestOtherFormats(TempProjects):
    def test_docx_embedded_and_legend_only(self):
        p = self.work / "paper.docx"
        doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
               '<w:p><w:r><w:t>Results show a big effect (Figure 1).</w:t></w:r></w:p>'
               '<w:p><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p>'
               '<w:p><w:pPr><w:pStyle w:val="Caption"/></w:pPr><w:r><w:t>Figure 1. Mean response; error bars SEM.</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>Figure 2. Dose response curves for all twelve animals in both conditions.</w:t></w:r></w:p>'
               '</w:body></w:document>')
        rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId5" Target="media/image1.png"/></Relationships>')
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("word/document.xml", doc)
            z.writestr("word/_rels/document.xml.rels", rels)
            z.writestr("word/media/image1.png", PNG_1PX)
        figs, _ = collect_figures(p, {"plots.py": 'plt.savefig("image1.png")\n'}, cache_dir=self.work / "c",
                                  manuscript_text="Results show a big effect (Figure 1).")
        self.assertEqual([f.number for f in figs], [1, 2])
        self.assertIn("error bars SEM", figs[0].caption)
        self.assertTrue(Path(figs[0].image).is_file())
        self.assertEqual(figs[0].code[0]["file"], "plots.py")
        self.assertEqual(figs[1].resolved, [])          # legend only; image submitted separately

    def test_separate_figure_files_and_markdown(self):
        (self.work / "figs").mkdir()
        (self.work / "figs" / "Figure_2.png").write_bytes(PNG_1PX)
        md = self.work / "paper.md"
        md.write_text("Text.\n\n![Panel A](a.png)\n\nFigure 1. Something.\n", encoding="utf-8")
        figs, _ = collect_figures(md, {}, figures_dir=self.work / "figs")
        self.assertEqual(figs[0].missing, ["a.png"])
        self.assertEqual(image_size(self.work / "figs" / "Figure_2.png"), (1, 1))

    def test_rmarkdown_chunks_are_figures(self):
        rmd = self.work / "paper.Rmd"
        rmd.write_text('Intro.\n\n```{r setup}\nlibrary(ggplot2)\n```\n\n'
                       '```{r refraction, fig.cap="Mean change; error bars SD."}\n'
                       'd <- read.csv("data/biometry.csv")\nggplot(d, aes(g, y)) + stat_summary(fun.data = mean_se)\n```\n\n'
                       '```{r}\n#| label: axial\n#| fig-cap: "Axial length by group."\nboxplot(y ~ g, d)\n```\n',
                       encoding="utf-8")
        out = self.work / "paper_files" / "figure-html"
        out.mkdir(parents=True)
        (out / "refraction-1.png").write_bytes(PNG_1PX)
        figs, _ = collect_figures(rmd, {})
        self.assertEqual([(f.number, f.label) for f in figs], [(1, "refraction"), (2, "axial")])
        self.assertTrue(figs[0].resolved[0].endswith("refraction-1.png"))
        self.assertEqual(figs[0].code[0]["reads"], ["biometry.csv"])
        self.assertEqual(figs[1].image_note, "not rendered yet (knit the document to check the image)")
        found = {f["title"] for f in check_figures(figs, code_provided=True)}
        self.assertIn("Figure 1: caption says SD but the code plots the standard error", found)

    def test_figure_output_patterns(self):
        outs = figure_outputs({
            "a.R": 'ggsave(filename = "out/f1.pdf", p)\npdf(file="f2.pdf")\n# png("ignored.png")\n',
            "b.py": "fig.savefig('f3.png', dpi=300)\n",
            "c.m": "print(gcf, '-dpng', 'f4.png')\nsaveas(gcf, 'f5.fig')\n",
        })
        self.assertEqual(sorted(o["stem"] for o in outs), ["f1", "f2", "f3", "f4", "f5"])


if __name__ == "__main__":
    unittest.main()
