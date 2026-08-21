import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "saige_reviewer" / "static"


class AnalysisDialogFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "analysis-dialog.css").read_text(encoding="utf-8")

    def test_dialog_owns_its_responsive_width_and_accessible_name(self):
        self.assertIn('href="/analysis-dialog.css"', self.html)
        self.assertIn(
            'id="analysisDialog" aria-labelledby="analysisDialogTitle"', self.html
        )
        self.assertIn('id="analysisDialogTitle">分析配置</h2>', self.html)
        self.assertIn('class="dialog-card analysis-card"', self.html)
        self.assertNotIn('class="dialog-card wide"', self.html)
        self.assertIn("#analysisDialog {", self.css)
        self.assertIn("width: min(700px, calc(100vw - 24px));", self.css)

    def test_form_never_scrolls_horizontally_and_collapses_to_one_column(self):
        self.assertIn("#analysisDialog .analysis-card {", self.css)
        self.assertIn("width: 100%;", self.css)
        self.assertIn("min-width: 0;", self.css)
        self.assertIn("overflow-x: hidden;", self.css)
        self.assertIn("overflow-y: auto;", self.css)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr));", self.css
        )
        self.assertIn("@media (max-width: 620px)", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", self.css)

    def test_status_and_actions_remain_accessible_while_scrolling(self):
        self.assertIn(
            'id="dependencyState" class="dependency" role="status" aria-live="polite"',
            self.html,
        )
        self.assertIn("#analysisDialog .dialog-actions {", self.css)
        self.assertIn("position: sticky;", self.css)


if __name__ == "__main__":
    unittest.main()
