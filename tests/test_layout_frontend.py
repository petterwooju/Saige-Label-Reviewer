import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "saige_reviewer" / "static"


class ResizableLayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "layout.css").read_text(encoding="utf-8")

    def test_three_panels_have_accessible_resize_handles(self):
        self.assertIn('href="/layout.css"', self.html)
        for panel in ("reviewPanel", "featurePanel", "detailPanel"):
            self.assertIn(f'id="{panel}"', self.html)
        for handle in ("leftResizer", "rightResizer"):
            self.assertIn(f'id="{handle}" class="column-resizer"', self.html)
        self.assertEqual(self.html.count('role="separator"'), 2)
        self.assertEqual(self.html.count('aria-orientation="vertical"'), 2)

    def test_layout_keeps_a_minimum_center_width(self):
        self.assertIn("--left-panel-width", self.css)
        self.assertIn("--right-panel-width", self.css)
        self.assertIn("minmax(300px, 1fr)", self.css)
        self.assertIn("#reviewPanel,", self.css)
        self.assertIn("min-width: 0", self.css)
        self.assertIn("PANEL_CENTER_MIN=300", self.script)
        self.assertIn("normalizePanelWidths", self.script)

    def test_drag_keyboard_reset_and_persistence_are_wired(self):
        self.assertIn("setPointerCapture(event.pointerId)", self.script)
        self.assertIn("handle.addEventListener('pointermove',moveColumnResize)", self.script)
        self.assertIn("handle.addEventListener('keydown',handleColumnResizeKey)", self.script)
        self.assertIn("handle.addEventListener('dblclick',resetColumnWidth)", self.script)
        self.assertIn("event.key==='Home'", self.script)
        self.assertIn("localStorage.setItem(PANEL_LAYOUT_KEY", self.script)
        self.assertIn("initializeColumnResizers();", self.script)
        self.assertIn("schedulePanelWidthClamp()", self.script)
        self.assertIn("function schedulePlotDraw(items=null){if(!state)return", self.script)

    def test_analysis_ui_explains_gpu_priority_and_reports_actual_device(self):
        self.assertIn("自动（优先 GPU，失败转 CPU）", self.html)
        self.assertIn("device==='cuda'?'GPU'", self.script)
        self.assertIn("GPU 不可用，已自动改用 CPU", self.script)

    def test_narrow_layout_keeps_analysis_and_detail_available(self):
        narrow = self.css[self.css.index("@media (max-width: 820px)") :]
        self.assertIn("#detailPanel", narrow)
        self.assertIn("display: block", narrow)
        self.assertIn("grid-column: 1 / -1", narrow)
        self.assertIn(".top-actions #openAnalysis", narrow)
        self.assertIn("overflow: visible", narrow)


if __name__ == "__main__":
    unittest.main()
