import hashlib
import io
import importlib.util
import math
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.analysis import (
    DEFAULT_MODEL_REVISION,
    AnalysisConfig,
    _build_item_updates,
    _assert_source_unchanged,
    _assert_analysis_inputs_unchanged,
    _forward_model,
    _is_cuda_runtime_failure,
    _load_model,
    _load_cache,
    _save_cache,
    _local_neighbor_distribution,
    _neighbor_candidates,
    _normalized_float32_features,
    _model_identity,
    _polygons,
    _prepare_image,
    _validate_analysis_scale,
    _effective_projection,
    _project_features,
    _preflight_projection,
    _preflight_scoring_dependencies,
    analysis_input_fingerprint,
    _normalized_bbox,
    analysis_fingerprint,
    model_status,
    preview_content_digest,
    score_feature_matrix,
)


class AnalysisConfigTests(unittest.TestCase):
    def test_accepts_all_required_modes_and_sizes(self):
        for mode in ("original", "dimmed", "neutral_outside"):
            for size in ("auto", "224", "336", "518"):
                config = AnalysisConfig.from_dict({"background_mode": mode, "input_size": size})
                self.assertEqual((config.background_mode, config.input_size), (mode, size))

    def test_defaults_to_pinned_offline_model(self):
        config = AnalysisConfig.from_dict({})
        self.assertEqual(config.model_revision, DEFAULT_MODEL_REVISION)
        self.assertEqual(config.model_access, "local_only")
        self.assertEqual(config.device, "auto")
        self.assertRegex(config.model_revision, r"^[0-9a-f]{40}$")

    def test_explicit_download_policy_is_accepted(self):
        config = AnalysisConfig.from_dict({"model_access": "download_if_missing"})
        self.assertEqual(config.model_access, "download_if_missing")

    def test_umap_is_never_silently_reported_when_tsne_was_used(self):
        with mock.patch.dict(sys.modules, {"umap": None}):
            with self.assertRaisesRegex(RuntimeError, "umap-learn"):
                _project_features([[0], [1], [2], [3]], "umap", lambda *_: None, object())

    def test_broken_umap_install_fails_during_preflight(self):
        with mock.patch("saige_reviewer.analysis.importlib.import_module",
                        side_effect=OSError("broken DLL")):
            with self.assertRaisesRegex(RuntimeError, "无法导入"):
                _preflight_projection("umap")

    def test_tiny_svd_projection_does_not_preflight_umap(self):
        with mock.patch("saige_reviewer.analysis.importlib.import_module",
                        side_effect=AssertionError("UMAP must not be imported")) as importer:
            _preflight_projection("umap", item_count=3)
        importer.assert_not_called()

    def test_broken_sklearn_install_fails_during_preflight(self):
        with mock.patch("saige_reviewer.analysis.importlib.import_module",
                        side_effect=OSError("broken scipy DLL")):
            with self.assertRaisesRegex(RuntimeError, "scikit-learn"):
                _preflight_scoring_dependencies()

    def test_tiny_dataset_reports_svd_as_the_effective_projection(self):
        self.assertEqual(_effective_projection("umap", 2), "svd")
        self.assertEqual(_effective_projection("tsne", 3), "svd")
        self.assertEqual(_effective_projection("umap", 4), "umap")

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_tiny_tsne_request_actually_uses_svd(self):
        import numpy as np

        features = np.asarray([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
        with mock.patch.dict(sys.modules, {"sklearn.manifold": None}):
            coords = _project_features(features, "tsne", lambda *_: None, np)
        self.assertEqual(coords.shape, (3, 2))
        self.assertTrue(np.isfinite(coords).all())

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_tiny_svd_coordinates_are_standard_u_times_s_scores(self):
        import numpy as np

        features = np.asarray([[0.0, 0.0], [4.0, 0.0]], dtype=np.float32)
        coords = _project_features(features, "umap", lambda *_: None, np)
        self.assertAlmostEqual(float(np.linalg.norm(coords[0] - coords[1])), 4.0, places=5)

    def test_rejects_mutable_or_invalid_model_revision(self):
        for revision in ("main", "latest", "f9e44c8", "z" * 40):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                AnalysisConfig.from_dict({"model_revision": revision})

    def test_rejects_unsafe_roi_expansion(self):
        for value in (True, 3, math.nan, math.inf, "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AnalysisConfig.from_dict({"roi_expansion": value})

    def test_rejects_boolean_batch_size(self):
        with self.assertRaises(ValueError):
            AnalysisConfig.from_dict({"batch_size": True})

    def test_oversized_analysis_is_rejected_before_allocating_matrices(self):
        with self.assertRaisesRegex(ValueError, "评分内存"):
            _validate_analysis_scale(10_000_000, 10_000, "umap")
        with self.assertRaisesRegex(ValueError, "t-SNE 最多"):
            _validate_analysis_scale(50_001, 2, "tsne")

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_float32_normalization_is_requested_in_place_without_a_copy(self):
        import numpy as np

        features = np.asarray([[3.0, 4.0], [0.0, 2.0]], dtype=np.float32)
        original = features

        def normalize_in_place(value, *, norm, copy):
            self.assertIs(value, original)
            self.assertEqual(norm, "l2")
            self.assertFalse(copy)
            value /= np.linalg.norm(value, axis=1, keepdims=True)
            return value

        result = _normalized_float32_features(features, np, normalize_in_place)
        self.assertIs(result, original)
        np.testing.assert_allclose(np.linalg.norm(result, axis=1), 1.0)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_float64_conversion_peak_is_guarded_before_allocating_output(self):
        import numpy as np

        features = np.ones((2, 4), dtype=np.float64)
        normalize = mock.Mock(side_effect=AssertionError("must not normalize"))
        # 64 bytes resident input + 32 bytes float32 output + 8 bytes row norms.
        with mock.patch("saige_reviewer.analysis.MAX_FEATURE_MATRIX_BYTES", 103):
            with self.assertRaisesRegex(ValueError, "峰值内存"):
                _normalized_float32_features(features, np, normalize)
        normalize.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_neighbor_voting_filters_self_even_when_ties_reorder_it(self):
        import numpy as np

        # Row 0's self index is deliberately second, as can happen for exact
        # duplicate features with tied zero cosine distance.
        indices = np.asarray([[1, 0, 2], [1, 0, 2], [2, 1, 0]])
        distances = np.zeros_like(indices, dtype=np.float32)
        labels = np.asarray([0, 1, 1], dtype=np.int64)
        local = _local_neighbor_distribution(
            indices, distances, labels, 2, neighbor_target=1, np=np
        )
        self.assertEqual(tuple(local[0]), (0.0, 1.0))
        self.assertEqual(tuple(local[1]), (1.0, 0.0))

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_large_neighbor_branch_uses_deterministic_nndescent_and_filters_self(self):
        import numpy as np

        captured = {}

        class FakeNNDescent:
            def __init__(self, features, **kwargs):
                captured.update(kwargs)
                self.neighbor_graph = (
                    np.asarray([[1, 0, 2], [1, 0, 2], [2, 1, 0]]),
                    np.zeros((3, 3), dtype=np.float32),
                )

        class ForbiddenExact:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("exact neighbor search must not run")

        fake_approximate = SimpleNamespace(NNDescent=FakeNNDescent)
        fake_exact = SimpleNamespace(NearestNeighbors=ForbiddenExact)
        with mock.patch("saige_reviewer.analysis.APPROXIMATE_NEIGHBOR_THRESHOLD", 2), \
                mock.patch.dict(sys.modules, {
                    "pynndescent": fake_approximate,
                    "sklearn.neighbors": fake_exact,
                }):
            indices, distances = _neighbor_candidates(np.eye(3, dtype=np.float32), 3)
        self.assertEqual(captured, {
            "n_neighbors": 3,
            "metric": "cosine",
            "random_state": 42,
            "n_jobs": 1,
            "low_memory": True,
        })
        local = _local_neighbor_distribution(
            indices, distances, np.asarray([0, 1, 1]), 2, neighbor_target=1, np=np
        )
        self.assertEqual(tuple(local[0]), (0.0, 1.0))

    def test_bbox_is_clamped_and_invalid_values_are_ignored(self):
        self.assertEqual(_normalized_bbox([-5, -2, 30, 40], 20, 25), (0.0, 0.0, 20.0, 25.0))
        for value in (
            [5, 5, 4, 6],
            [math.nan, 0, 2, 2],
            [50, 50, 60, 60],
            [1, 2, 3],
            "1,2,3,4",
        ):
            with self.subTest(value=value):
                self.assertIsNone(_normalized_bbox(value, 20, 25))

    def test_three_disconnected_contours_remain_three_polygons(self):
        contours = [
            [[0, 0], [1, 0], [1, 1]],
            [[10, 10], [11, 10], [11, 11]],
            [[20, 20], [21, 20], [21, 21]],
        ]
        self.assertEqual(len(_polygons(contours)), 3)

    def test_local_model_preflight_requires_config_and_safe_weights(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            model_dir = Path(directory)
            config = AnalysisConfig.from_dict({"model": str(model_dir)})
            self.assertFalse(model_status(config)["ready"])
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_bytes(b"fixture")
            status = model_status(config)
            self.assertTrue(status["ready"])
            self.assertTrue(status["cached"])

    def test_download_loader_uses_pinned_revision_and_safe_weights(self):
        captured = {}

        class FakeAutoModel:
            @staticmethod
            def from_pretrained(model, **kwargs):
                captured.update({"model": model, **kwargs})
                return "loaded"

        config = AnalysisConfig.from_dict({"model_access": "download_if_missing"})
        loaded = _load_model(
            config,
            "float32",
            FakeAutoModel,
            {"ready": True, "cached": False, "download_allowed": True},
        )
        self.assertEqual(loaded, "loaded")
        self.assertEqual(captured["revision"], DEFAULT_MODEL_REVISION)
        self.assertEqual(captured["dtype"], "float32")
        self.assertNotIn("torch_dtype", captured)
        self.assertFalse(captured["local_files_only"])
        self.assertTrue(captured["use_safetensors"])
        self.assertFalse(captured["trust_remote_code"])

    def test_model_forward_enables_positional_interpolation_when_supported(self):
        class SupportingModel:
            def forward(self, pixel_values, interpolate_pos_encoding=False):
                return SimpleNamespace(
                    pixel_values=pixel_values,
                    interpolate_pos_encoding=interpolate_pos_encoding,
                )

            __call__ = forward

        output = _forward_model(SupportingModel(), "pixels")
        self.assertEqual(output.pixel_values, "pixels")
        self.assertTrue(output.interpolate_pos_encoding)

    def test_model_forward_omits_positional_interpolation_when_unsupported(self):
        class LegacyModel:
            def forward(self, pixel_values):
                return SimpleNamespace(pixel_values=pixel_values)

            __call__ = forward

        class ForwardingWrapper:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.forwarded_kwargs = None

            def forward(self, pixel_values, **kwargs):
                self.forwarded_kwargs = kwargs
                return self.wrapped(pixel_values=pixel_values, **kwargs)

            __call__ = forward

        model = ForwardingWrapper(LegacyModel())
        output = _forward_model(model, "pixels")
        self.assertEqual(output.pixel_values, "pixels")
        self.assertEqual(model.forwarded_kwargs, {})

    def test_model_forward_does_not_swallow_internal_type_error(self):
        class BrokenModel:
            calls = 0

            def forward(self, pixel_values, interpolate_pos_encoding=False):
                self.calls += 1
                raise TypeError("failure inside model")

            __call__ = forward

        model = BrokenModel()
        with self.assertRaisesRegex(TypeError, "failure inside model"):
            _forward_model(model, "pixels")
        self.assertEqual(model.calls, 1)

    def test_cuda_fallback_only_classifies_device_runtime_failures(self):
        class FakeCudaOutOfMemory(RuntimeError):
            pass

        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(OutOfMemoryError=FakeCudaOutOfMemory)
        )
        self.assertTrue(
            _is_cuda_runtime_failure(FakeCudaOutOfMemory("allocation failed"), fake_torch)
        )
        self.assertTrue(
            _is_cuda_runtime_failure(RuntimeError("CUDA driver error"), fake_torch)
        )
        self.assertFalse(
            _is_cuda_runtime_failure(RuntimeError("模型文件在加载期间发生变化"), fake_torch)
        )
        self.assertFalse(
            _is_cuda_runtime_failure(ValueError("输入图像无效"), fake_torch)
        )

    def test_cache_fingerprint_includes_revision_dependencies_and_items(self):
        item = SimpleNamespace(id="1", relative_path="a.jpg", original_label="ok")
        dataset = SimpleNamespace(source_hash="source", classes=["ok", "bad"], items=[item])
        first = AnalysisConfig.from_dict({})
        second = AnalysisConfig.from_dict({"model_revision": "a" * 40})
        baseline = analysis_fingerprint(dataset, first, 224, {"torch": "2.6.0"})
        self.assertNotEqual(
            baseline, analysis_fingerprint(dataset, second, 224, {"torch": "2.6.0"})
        )
        self.assertNotEqual(
            baseline, analysis_fingerprint(dataset, first, 224, {"torch": "2.7.0"})
        )
        self.assertNotEqual(
            analysis_fingerprint(
                dataset,
                first,
                224,
                {"torch": "2.6.0"},
                execution_identity={"device": "cpu", "dtype": "float32"},
            ),
            analysis_fingerprint(
                dataset,
                first,
                224,
                {"torch": "2.6.0"},
                execution_identity={"device": "cuda", "dtype": "float16"},
            ),
        )
        item.original_label = "bad"
        self.assertNotEqual(
            baseline, analysis_fingerprint(dataset, first, 224, {"torch": "2.6.0"})
        )

    def test_local_model_fingerprint_hashes_weight_contents(self):
        item = SimpleNamespace(id="1", relative_path="a.jpg", original_label="ok")
        dataset = SimpleNamespace(source_hash="source", classes=["ok"], items=[item])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            model_dir = Path(directory)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            weights = model_dir / "model.safetensors"
            weights.write_bytes(b"first")
            config = AnalysisConfig.from_dict({"model": str(model_dir)})
            first = analysis_fingerprint(dataset, config, 224, {"torch": "2.6.0"})
            stat = weights.stat()
            weights.write_bytes(b"other")
            os.utime(weights, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            second = analysis_fingerprint(dataset, config, 224, {"torch": "2.6.0"})
            self.assertNotEqual(first, second)

    def test_huggingface_model_identity_hashes_cached_file_contents(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            config_path = root / "config.json"
            weight_path = root / "model.safetensors"
            config_path.write_bytes(b"config-a")
            weight_path.write_bytes(b"weights-a")
            paths = {"config.json": str(config_path), "model.safetensors": str(weight_path)}
            fake_hub = SimpleNamespace(
                try_to_load_from_cache=lambda _model, name, **_kwargs: paths[name]
            )
            config = AnalysisConfig()
            with mock.patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
                first = _model_identity(config)
                stat = weight_path.stat()
                weight_path.write_bytes(b"weights-b")
                os.utime(weight_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                second = _model_identity(config)
            self.assertNotEqual(first, second)
            self.assertEqual({entry["name"] for entry in second["files"]},
                             {"config.json", "model.safetensors"})

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_cache_header_is_validated_before_numpy_can_allocate(self):
        import numpy as np

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "malicious.npz"

            def header(shape):
                stream = io.BytesIO()
                np.lib.format.write_array_header_1_0(
                    stream,
                    {"descr": np.dtype("float32").str,
                     "fortran_order": False, "shape": shape},
                )
                return stream.getvalue()

            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("coords.npy", header((1_000_000_000, 2)))
                for name in ("scores", "predicted", "confidence", "support"):
                    archive.writestr(f"{name}.npy", header((2,)))
            with mock.patch.object(np, "load", side_effect=AssertionError("must not allocate")) as load:
                self.assertIsNone(_load_cache(path, 2, 2, np))
            load.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
    def test_valid_cache_passes_the_archive_preflight(self):
        import numpy as np

        arrays = {
            "coords": np.asarray([[0, 1], [2, 3]], dtype=np.float32),
            "scores": np.asarray([10, 20], dtype=np.float32),
            "predicted": np.asarray([0, 1], dtype=np.int64),
            "confidence": np.asarray([0.9, 0.8], dtype=np.float32),
            "support": np.asarray([0.7, 0.6], dtype=np.float32),
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "valid.npz"
            _save_cache(path, arrays, np)
            loaded = _load_cache(path, 2, 2, np)
        self.assertIsNotNone(loaded)
        np.testing.assert_array_equal(loaded["coords"], arrays["coords"])

    def test_item_updates_do_not_mutate_source_items(self):
        item = SimpleNamespace(id="sample", x=99.0, suspicion_score=None)
        updates = _build_item_updates(
            [item], ["ok", "bad"], [[1.5, -2]], [87.123], [1], [0.128765], [0.25]
        )
        self.assertEqual((item.x, item.suspicion_score), (99.0, None))
        self.assertEqual(
            updates[0],
            {
                "id": "sample",
                "x": 1.5,
                "y": -2.0,
                "suspicion_score": 87.12,
                "suggested_label": "bad",
                "label_confidence": 0.12876,
                "neighbor_support": 0.25,
                "analysis_state": "analyzed",
            },
        )

    def test_source_change_is_rejected_with_reload_guidance(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / "dataset.json"
            source.write_bytes(b"first")
            dataset = SimpleNamespace(
                source=source,
                source_hash=hashlib.sha256(b"first").hexdigest(),
            )
            _assert_source_unchanged(dataset)
            source.write_bytes(b"other")
            with self.assertRaisesRegex(RuntimeError, "重新载入"):
                _assert_source_unchanged(dataset)

    def test_project_image_contents_are_part_of_analysis_fingerprint(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "fixture.srproj"
            image = root / "image.jpg"
            source.write_bytes(b"project")
            image.write_bytes(b"first")
            item = SimpleNamespace(
                id="1", relative_path="image.jpg", original_label="ok", metadata={}
            )
            dataset = SimpleNamespace(
                source=source,
                source_hash=hashlib.sha256(b"project").hexdigest(),
                source_type="srproj:detection",
                classes=["ok"],
                items=[item],
                metadata={},
            )
            config = AnalysisConfig.from_dict({})
            initial_inputs = analysis_input_fingerprint(dataset)
            initial_cache = analysis_fingerprint(
                dataset, config, 224, {"torch": "2.6.0"}
            )

            stat = image.stat()
            image.write_bytes(b"other")
            os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns))

            self.assertNotEqual(initial_inputs, analysis_input_fingerprint(dataset))
            self.assertNotEqual(
                initial_cache, analysis_fingerprint(dataset, config, 224, {"torch": "2.6.0"})
            )

    def test_missing_external_image_is_rejected_during_input_preflight(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "fixture.srproj"
            source.write_text("<Project/>", encoding="utf-8")
            dataset = SimpleNamespace(
                source=source,
                source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
                source_type="srproj:detection",
                items=[SimpleNamespace(relative_path="missing.jpg", metadata={})],
                metadata={},
            )
            with self.assertRaisesRegex(RuntimeError, "missing.jpg"):
                analysis_input_fingerprint(dataset)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_analysis_image_preparation_rejects_pixel_bombs_before_conversion(self):
        from PIL import Image, ImageDraw, ImageEnhance

        output = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(output, "PNG")
        item = SimpleNamespace(relative_path="large.png", metadata={})
        config = AnalysisConfig()
        with mock.patch("saige_reviewer.analysis.read_preview",
                        return_value=(output.getvalue(), "image/png")), \
                mock.patch("saige_reviewer.analysis.MAX_IMAGE_PIXELS", 3), \
                mock.patch.object(Image.Image, "convert",
                                  side_effect=AssertionError("must not convert")):
            with self.assertRaisesRegex(ValueError, "像素数超过"):
                _prepare_image(
                    SimpleNamespace(), item, 224, config,
                    Image, ImageDraw, ImageEnhance,
                )

    def test_duplicate_annotations_hash_each_image_once(self):
        items = [
            SimpleNamespace(relative_path="images/a.jpg", metadata={}),
            SimpleNamespace(relative_path="images\\a.jpg", metadata={}),
        ]
        dataset = SimpleNamespace(
            source_hash="project",
            source_type="saige-json:detection",
            items=items,
        )
        with mock.patch(
            "saige_reviewer.analysis.read_preview", return_value=(b"image", "image/jpeg")
        ) as reader:
            analysis_input_fingerprint(dataset)
        self.assertEqual(reader.call_count, 1)

    def test_preview_content_digest_and_item_update_share_private_snapshot_hash(self):
        item = SimpleNamespace(
            id="sample", relative_path="image.jpg", metadata={}, x=0.0,
            suspicion_score=None,
        )
        dataset = SimpleNamespace()
        with mock.patch("saige_reviewer.analysis.read_preview",
                        return_value=(b"image-bytes", "image/jpeg")):
            digest = preview_content_digest(dataset, item)
        self.assertEqual(digest, hashlib.sha256(b"image-bytes").hexdigest())
        updates = _build_item_updates(
            [item], ["ok"], [[1.0, 2.0]], [10.0], [0], [0.9], [0.8],
            {"image.jpg": digest},
        )
        self.assertEqual(updates[0]["_analysis_preview_sha256"], digest)


@unittest.skipUnless(
    importlib.util.find_spec("numpy") is not None
    and importlib.util.find_spec("sklearn") is not None,
    "需要可选的 numpy/scikit-learn 分析依赖",
)
class FeatureScoringValidationTests(unittest.TestCase):
    @staticmethod
    def _clusters(samples_per_class=30, noise=0.025):
        import numpy as np

        random = np.random.default_rng(42)
        centers = np.eye(3, 6, dtype=np.float32)
        features = np.concatenate(
            [center + random.normal(0, noise, size=(samples_per_class, 6))
             for center in centers]
        )
        labels = np.repeat(np.arange(3), samples_per_class)
        return features, labels

    def test_injected_wrong_label_is_ranked_near_the_top(self):
        import numpy as np

        features, labels = self._clusters()
        injected_index = 0
        labels[injected_index] = 1

        result = score_feature_matrix(features, labels)

        self.assertEqual(int(result["predicted"][injected_index]), 0)
        self.assertGreaterEqual(
            float(result["scores"][injected_index]), float(np.percentile(result["scores"], 95))
        )

    def test_multiple_injected_errors_have_high_precision_and_recall_at_top_k(self):
        import numpy as np

        features, labels = self._clusters(samples_per_class=40, noise=0.04)
        injected = np.asarray([0, 1, 40, 41, 80, 81])
        labels[injected] = (labels[injected] + 1) % 3
        result = score_feature_matrix(features, labels)
        order = np.argsort(-result["scores"])
        top_six = set(map(int, order[:6]))
        top_twelve = set(map(int, order[:12]))
        truth = set(map(int, injected))
        self.assertGreaterEqual(len(top_six & truth) / 6, 5 / 6)
        self.assertEqual(len(top_twelve & truth) / len(truth), 1.0)

    def test_singleton_and_imbalanced_classes_remain_finite_and_bounded(self):
        import numpy as np

        random = np.random.default_rng(7)
        features = np.concatenate([
            np.asarray([1, 0, 0, 0], dtype=np.float32) + random.normal(0, .05, (40, 4)),
            np.asarray([0, 1, 0, 0], dtype=np.float32) + random.normal(0, .05, (4, 4)),
            np.asarray([[0, 0, 1, 0]], dtype=np.float32),
        ])
        labels = np.asarray([0] * 40 + [1] * 4 + [2])
        result = score_feature_matrix(features, labels)
        self.assertTrue(np.isfinite(result["scores"]).all())
        self.assertTrue(((result["scores"] >= 0) & (result["scores"] <= 100)).all())
        self.assertEqual(len(result["predicted"]), len(labels))


if __name__ == "__main__":
    unittest.main()
