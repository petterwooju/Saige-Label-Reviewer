import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "saige_reviewer" / "static"


class SourcePickerFrontendTests(unittest.TestCase):
    def test_dialog_exposes_file_folder_and_external_root_pickers(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="sourceDialog" aria-labelledby="sourceDialogTitle"', html)
        self.assertIn('id="sourceForm"', html)
        self.assertIn('id="pickSourceFile" type="button"', html)
        self.assertIn('id="pickSourceFolder" type="button"', html)
        self.assertIn('id="pickPreviewRoot" type="button"', html)
        self.assertIn('id="confirmOpen" type="submit"', html)
        self.assertIn('id="sourceError"', html)

    def test_dialog_submit_and_picker_share_validated_frontend_flow(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("post('/api/pick-path',{kind,purpose})", script)
        self.assertIn("if(value.cancelled||!value.path)return", script)
        self.assertIn("'preview_root'", script)
        self.assertIn("parsePreviewRoots()", script)
        self.assertIn("if(dialog.open)$('#sourceInput').focus()", script)
        self.assertIn("$('#sourceForm').addEventListener('submit',confirmOpen)", script)
        self.assertIn("requestSubmit($('#confirmOpen'))", script)
        self.assertIn("setSourceError(error.message", script)
        self.assertIn("setSourceControlsDisabled(true)", script)


if __name__ == "__main__":
    unittest.main()
