import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "saige_reviewer" / "static"


class PlotFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "plot.css").read_text(encoding="utf-8")

    def test_feature_map_exposes_mouse_and_keyboard_controls(self):
        self.assertIn('id="plot" tabindex="0"', self.html)
        self.assertIn('aria-describedby="plotHelp plotZoomStatus"', self.html)
        for control in ("zoomOutPlot", "plotZoomStatus", "zoomInPlot", "fitPlot"):
            self.assertIn(f'id="{control}"', self.html)
        self.assertIn("鼠标：滚轮缩放、拖拽平移、点击选择", self.html)
        self.assertIn("键盘：＋/－ 缩放，方向键平移，0 或 F 自适应", self.html)
        self.assertIn("#plot:focus-visible", self.css)
        self.assertIn("touch-action: none", self.css)

    def test_pointer_gestures_capture_and_do_not_turn_drags_into_clicks(self):
        self.assertIn("PLOT_DRAG_THRESHOLD=5", self.script)
        self.assertIn("setPointerCapture(event.pointerId)", self.script)
        self.assertIn("releasePointerCapture(event.pointerId)", self.script)
        self.assertIn("addEventListener('pointercancel'", self.script)
        self.assertIn("addEventListener('lostpointercapture'", self.script)
        self.assertIn("suppressPlotClickUntil=Date.now()+300", self.script)
        self.assertIn("addEventListener('wheel',handlePlotWheel,{passive:false})", self.script)

    def test_rendering_is_batched_high_dpi_bounded_and_resize_aware(self):
        self.assertIn("requestAnimationFrame", self.script)
        self.assertIn("new ResizeObserver(()=>schedulePlotDraw())", self.script)
        self.assertIn("Math.sqrt(12000000/area)", self.script)
        self.assertIn("Math.min(dpr,2.5,maxByArea,maxByEdge)", self.script)
        self.assertIn("pointHitGrid", self.script)
        self.assertIn("buckets=new Map()", self.script)

    def test_sampling_and_empty_states_are_explicit(self):
        self.assertIn("buildPlotSample(items,limit)", self.script)
        self.assertIn("stablePlotHash", self.script)
        self.assertIn("分层抽样", self.script)
        self.assertIn("当前筛选没有样本", self.script)
        self.assertIn("样本尚无特征坐标，请先运行分析", self.script)
        self.assertIn('id="plotStats"', self.html)

    def test_sampling_cache_is_stable_across_selection_and_refits_on_filters(self):
        self.assertIn("plotSampleBase=items.length<=PLOT_LIMIT?items:buildPlotSample", self.script)
        self.assertIn("plotSampleIds=new Set(plotSampleBase.map", self.script)
        self.assertIn("if(injectedId!==plotSampleInjectedId)", self.script)
        self.assertIn("plotSampleView[slot]=injectedId?selected:plotSampleBase[slot]", self.script)
        self.assertNotIn("current===plotSampleCurrent", self.script)
        self.assertIn("invalidateFiltered();resetPlotFit();", self.script)

    def test_plot_math_module_is_loaded_before_application(self):
        self.assertLess(self.html.index('/plot-math.js'), self.html.index('/app.js'))


class PlotMathTests(unittest.TestCase):
    def test_pointer_centered_zoom_and_degenerate_fit(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available on PATH")
        module = STATIC / "plot-math.js"
        program = r"""
const assert=require('node:assert/strict');
const math=require(process.argv[1]);
const bounds={minX:0,maxX:10,minY:0,maxY:10};
const viewport={width:800,height:600};
const view={zoom:1,panX:17,panY:-9};
const item={x:7,y:3};
const pointer=math.fittedPoint(item,bounds,viewport,view);
const zoomed=math.zoomViewAt(view,3,pointer,viewport,.35,40);
const after=math.fittedPoint(item,bounds,viewport,zoomed);
assert.ok(Math.abs(pointer.x-after.x)<1e-9);
assert.ok(Math.abs(pointer.y-after.y)<1e-9);
assert.equal(math.zoomViewAt(view,100,pointer,viewport,.35,40).zoom,40);
const centered=math.fittedPoint({x:4,y:8},{minX:4,maxX:4,minY:8,maxY:8},viewport,{zoom:1,panX:0,panY:0});
assert.deepEqual(centered,{x:400,y:300});
assert.equal(math.stablePlotHash('same-id'),math.stablePlotHash('same-id'));
assert.ok(math.adaptiveGridStep(.01)>=28);
assert.ok(math.adaptiveGridStep(100)<=84);
"""
        result = subprocess.run(
            [node, "-e", program, str(module)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
