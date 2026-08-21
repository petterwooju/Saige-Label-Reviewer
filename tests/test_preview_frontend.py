import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "saige_reviewer" / "static"


class PreviewFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "preview.css").read_text(encoding="utf-8")

    def test_preview_exposes_zoom_fit_mode_and_keyboard_controls(self):
        self.assertIn('id="preview" class="preview" tabindex="0"', self.html)
        self.assertIn('aria-describedby="previewHelp"', self.html)
        for control in (
            "previewZoomOut", "previewZoomStatus", "previewZoomIn",
            "fitPreview", "previewModeToggle", "previewContourToggle",
            "previewSizeLabel",
        ):
            self.assertIn(f'id="{control}"', self.html)
        self.assertIn("点击图片打开大图 · 可隐藏轮廓 · 滚轮缩放 · 拖拽平移", self.html)
        self.assertIn("handlePreviewKeydown", self.script)

    def test_preview_height_tracks_width_at_a_fixed_ratio(self):
        self.assertIn('id="previewComparison" class="preview-comparison"', self.html)
        self.assertIn("width: calc(100% - 30px)", self.css)
        self.assertIn("height: auto", self.css)
        self.assertIn("aspect-ratio: 4 / 3", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1.15fr) minmax(0, .85fr)", self.css)
        self.assertNotIn("height: 245px", self.css)

    def test_pointer_centered_zoom_drag_and_resize_are_wired(self):
        self.assertIn("zoomViewAt(previewView", self.script)
        self.assertIn("PREVIEW_DRAG_THRESHOLD=4", self.script)
        self.assertIn("setPointerCapture(event.pointerId)", self.script)
        self.assertIn("preview.addEventListener('pointercancel'", self.script)
        self.assertIn("preview.addEventListener('lostpointercapture'", self.script)
        self.assertIn("preview.addEventListener('wheel',handlePreviewWheel,{passive:false})", self.script)
        self.assertIn("new ResizeObserver(handlePreviewResize).observe(preview)", self.script)
        self.assertIn("touch-action: none", self.css)
        self.assertIn("cursor: grabbing", self.css)

    def test_item_or_mode_change_resets_view_without_hijacking_click(self):
        self.assertIn("image.dataset.previewKey!==key", self.script)
        self.assertIn("resetPreviewView();image.src=url", self.script)
        self.assertIn("$('#previewModeToggle').addEventListener('click',togglePreviewMode)", self.script)
        self.assertNotIn("$('#preview').addEventListener('click'", self.script)

    def test_same_class_reference_gallery_is_explicit_and_not_presented_as_truth(self):
        for control in ("referencePane", "referenceClass", "referenceSuggested", "referenceGallery"):
            self.assertIn(f'id="{control}"', self.html)
        self.assertIn("参考样本来自当前数据集，并非标准答案", self.html)
        self.assertIn("candidate.id!==item.id&&candidate.label===target", self.script)
        self.assertIn("sort(compareReferenceCandidates).slice(0,3)", self.script)
        self.assertIn("left.status==='correct'?0:1", self.script)
        self.assertIn("疑似分数", self.script)
        self.assertIn("reference-card:first-child", self.css)

    def test_reference_class_and_suggested_class_can_be_compared_safely(self):
        self.assertIn("referenceClassOverride=event.target.value", self.script)
        self.assertIn("对照建议：", self.script)
        self.assertIn("返回当前类：", self.script)
        self.assertIn("handleReferencePreviewError", self.script)
        self.assertIn("diagnosticUrl.searchParams.set('diagnose','1')", self.script)
        self.assertIn("detail.analysis_stale", self.script)

    def test_blocked_preview_offers_live_folder_authorization_and_retry(self):
        self.assertIn("function authorizePreviewRoot", self.script)
        self.assertIn("'/api/pick-path',{kind:'folder',purpose:'preview_root'}", self.script)
        self.assertIn("'/api/authorize-preview-root'", self.script)
        self.assertIn("showPreviewPermissionError(fallback,detail.error)", self.script)
        self.assertIn("showReferencePermissionError(card,detail.error)", self.script)
        self.assertIn("delete $('#preview img').dataset.previewKey", self.script)
        self.assertIn("preview-authorize-button", self.css)
        self.assertIn("reference-authorize-button", self.css)

    def test_all_comparison_images_open_the_large_image_viewer(self):
        for control in (
            "imageViewerDialog", "imageViewerStage", "imageViewerImage",
            "imageViewerPrevious", "imageViewerNext", "imageViewerZoomOut",
            "imageViewerZoom", "imageViewerZoomIn", "imageViewerFit",
            "imageViewerContourToggle", "imageViewerSize", "imageViewerClose",
        ):
            self.assertIn(f'id="{control}"', self.html)
        self.assertIn("comparisonViewerEntries=[{", self.script)
        self.assertIn("...candidates.map(candidate=>", self.script)
        self.assertIn("openImageViewer(index+1)", self.script)
        self.assertIn("openImageViewer(0)", self.script)
        self.assertIn("previewSuppressOpenUntil=Date.now()+300", self.script)

    def test_large_image_viewer_supports_zoom_pan_fit_and_keyboard_navigation(self):
        self.assertIn("IMAGE_VIEWER_MAX_ZOOM=20", self.script)
        self.assertIn("zoomViewAt(imageViewerView", self.script)
        self.assertIn("setPointerCapture(event.pointerId)", self.script)
        self.assertIn("imageViewerStage.addEventListener('pointercancel'", self.script)
        self.assertIn("imageViewerStage.addEventListener('wheel',handleImageViewerWheel,{passive:false})",
                      self.script)
        self.assertIn("imageViewerStage.addEventListener('dblclick',resetImageViewerView)", self.script)
        self.assertIn("event.key==='PageUp'", self.script)
        self.assertIn("event.key==='PageDown'", self.script)
        self.assertIn("width: min(1180px, calc(100vw - 24px))", self.css)
        self.assertIn("height: min(860px, calc(100dvh - 24px))", self.css)
        self.assertIn("touch-action: none", self.css)

    def test_all_image_browsers_share_a_contour_toggle_without_resetting_view(self):
        self.assertIn("showContours=true", self.script)
        self.assertIn("contours=${showContours?'1':'0'}", self.script)
        self.assertIn("$('#previewContourToggle').addEventListener('click',toggleContours)",
                      self.script)
        self.assertIn("$('#imageViewerContourToggle').addEventListener('click',toggleContours)",
                      self.script)
        self.assertIn("renderPreview(item,true)", self.script)
        self.assertIn("image.dataset.preserveView='true'", self.script)
        self.assertIn("if(preserve){clampImageViewerPan();updateImageViewerView()}", self.script)
        self.assertIn('#previewContourToggle[aria-pressed="true"]', self.css)

    def test_all_image_browsers_report_loaded_pixel_dimensions(self):
        self.assertIn("function imagePixelSize(image)", self.script)
        self.assertIn("`${image.naturalWidth} x ${image.naturalHeight} pix`", self.script)
        self.assertIn("$('#previewSizeLabel').textContent=imagePixelSize(event.currentTarget)",
                      self.script)
        self.assertIn("$('#imageViewerSize').textContent=imagePixelSize(image)", self.script)
        self.assertIn("dimensions.textContent=imagePixelSize(image)", self.script)
        self.assertIn("reference-dimensions", self.script)
        self.assertIn("#previewSizeLabel", self.css)
        self.assertIn("#imageViewerSize", self.css)
        self.assertIn(".reference-dimensions", self.css)


if __name__ == "__main__":
    unittest.main()
