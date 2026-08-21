import io
import json
import importlib.util
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import saige_reviewer.adapters as adapters
from saige_reviewer.adapters import (folder_fingerprint, load_folder, load_saige_json, load_srproj,
                                     load_visionproj, read_preview, render_preview)


PROJECT = {"project": {"projectName": "fixture", "projectType": "det",
    "classInfos": [{"className": "scratch"}], "projectFiles": [{"filePath": "images/a.jpg",
    "className": "scratch", "labelDataList": [{"labelId": 7, "className": "scratch",
    "labelPosX": 1, "labelPosY": 2, "labelWidth": 3, "labelHeight": 4}]}]}}


class AdapterTests(unittest.TestCase):
    def test_saige_json_detection(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.json"
            path.write_text(json.dumps(PROJECT), encoding="utf-8")
            dataset = load_saige_json(path)
            self.assertEqual(dataset.source_type, "saige-json:det")
            self.assertEqual(dataset.items[0].metadata["label_id"], 7)
            self.assertIsNone(dataset.items[0].suspicion_score)
            self.assertIsNone(dataset.items[0].suggested_label)
            self.assertEqual(dataset.items[0].analysis_state, "not_analyzed")

    def test_plain_project_size_limit_is_checked_before_json_parsing(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "oversized.json"
            path.write_bytes(b"x" * 9)
            with mock.patch("saige_reviewer.adapters.MAX_PROJECT_BYTES", 8), \
                    mock.patch("saige_reviewer.adapters.strict_json_loads") as loads:
                with self.assertRaisesRegex(ValueError, "\u8bfb\u53d6\u4e0a\u9650"):
                    load_saige_json(path)
            loads.assert_not_called()

    def test_json_loader_rejects_a_source_changed_after_its_snapshot_was_parsed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "changing.json"
            path.write_text(json.dumps(PROJECT), encoding="utf-8")
            real_loads = adapters.strict_json_loads

            def parse_then_replace(payload):
                value = real_loads(payload)
                path.write_text(json.dumps({"project": {"projectFiles": []}}), encoding="utf-8")
                return value

            with mock.patch("saige_reviewer.adapters.strict_json_loads",
                            side_effect=parse_then_replace):
                with self.assertRaisesRegex(OSError, "\u89e3\u6790\u671f\u95f4.*\u53d1\u751f\u53d8\u5316"):
                    load_saige_json(path)

    def test_visionproj_reads_manifest_and_preview_without_extracting(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sample.json", json.dumps(PROJECT))
                archive.writestr("images/a.jpg", b"jpeg-fixture")
            dataset = load_visionproj(path)
            self.assertEqual(dataset.source_type, "visionproj:det")
            self.assertEqual(read_preview(dataset, dataset.items[0]), (b"jpeg-fixture", "image/jpeg"))

    def test_visionproj_skips_unrelated_json_before_project_manifest(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("metadata.json", json.dumps({"version": 1}))
                archive.writestr("project/sample.json", json.dumps(PROJECT))
                archive.writestr("images/a.jpg", b"jpeg-fixture")
            dataset = load_visionproj(path)
            self.assertEqual(dataset.metadata["manifest"], "project/sample.json")

    def test_visionproj_rejects_multiple_valid_project_manifests(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "ambiguous.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("project.json", json.dumps(PROJECT))
                archive.writestr("backup/project.json", json.dumps(PROJECT))
                archive.writestr("images/a.jpg", b"jpeg-fixture")
            with self.assertRaisesRegex(ValueError, "多个有效项目清单"):
                load_visionproj(path)

    def test_visionproj_rejects_excessive_cumulative_json_candidate_bytes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "manifest-budget.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("one.json", b"{}{}")
                archive.writestr("two.json", b"{}{}")
                archive.writestr("three.json", b"{}{}")
            with mock.patch("saige_reviewer.adapters.MAX_TOTAL_MANIFEST_BYTES", 10), \
                    mock.patch("saige_reviewer.adapters.strict_json_loads") as loads:
                with self.assertRaisesRegex(ValueError, "累计.*超过"):
                    load_visionproj(path)
            loads.assert_not_called()

    def test_classification_srproj_uses_image_level_class(self):
        xml = """<?xml version='1.0'?><Project><Type>Classification</Type><ClassGroup>
        <Class><Name>ok</Name></Class></ClassGroup><ImageGroup><Image><Path>a.jpg</Path>
        <ClassIndexOfLabel>0</ClassIndexOfLabel></Image></ImageGroup></Project>"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.srproj"
            path.write_text(xml, encoding="utf-8")
            dataset = load_srproj(path)
            self.assertEqual((dataset.source_type, len(dataset.items)), ("srproj:classification", 1))
            self.assertIsNone(dataset.items[0].suspicion_score)
            self.assertEqual(dataset.items[0].metadata,
                             {"image_no": 0, "label_no": None})

    def test_srproj_object_items_keep_stable_image_and_label_ordinals(self):
        xml = """<Project><Type>Detection</Type><ClassGroup>
        <Class><Name>ok</Name></Class></ClassGroup><ImageGroup><Image><Path>a.jpg</Path>
        <LabelGroup><Label><ClassIndex>0</ClassIndex></Label>
        <Label><ClassIndex>0</ClassIndex></Label></LabelGroup></Image></ImageGroup></Project>"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "ordinals.srproj"
            path.write_text(xml, encoding="utf-8")
            dataset = load_srproj(path)
            self.assertEqual(
                [(item.metadata["image_no"], item.metadata["label_no"])
                 for item in dataset.items],
                [(0, 0), (0, 1)],
            )

    def test_srproj_class_index_ignores_comments_but_rejects_split_digits(self):
        template = ("<Project><Type>Detection</Type><ClassGroup>"
                    "<Class><Name>zero</Name></Class><Class><Name>one</Name></Class>"
                    "</ClassGroup><ImageGroup><Image><Path>a.jpg</Path><LabelGroup>"
                    "<Label><ClassIndex>{}</ClassIndex></Label>"
                    "</LabelGroup></Image></ImageGroup></Project>")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "commented-index.srproj"
            path.write_text(template.format("<!-- keep -->1"), encoding="utf-8")
            self.assertEqual(load_srproj(path).items[0].label, "one")
            path.write_text(template.format("1<!-- split -->0"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "多个非空文本片段"):
                load_srproj(path)

    def test_srproj_rejects_duplicate_display_names(self):
        xml = """<?xml version='1.0'?><Project><Type>Classification</Type><ClassGroup>
        <Class><Name>(0)Cat</Name></Class><Class><Name>(1)Cat</Name></Class>
        </ClassGroup><ImageGroup/></Project>"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "ambiguous.srproj"
            path.write_text(xml, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复.*类别显示名"):
                load_srproj(path)

    def test_srproj_preserves_a_non_numeric_parenthesized_class_name(self):
        xml = """<?xml version='1.0'?><Project><Type>Classification</Type><ClassGroup>
        <Class><Name>(damaged)Cat</Name></Class></ClassGroup><ImageGroup><Image>
        <Path>a.jpg</Path><ClassIndexOfLabel>0</ClassIndexOfLabel></Image></ImageGroup></Project>"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "literal-name.srproj"
            path.write_text(xml, encoding="utf-8")
            self.assertEqual(load_srproj(path).items[0].label, "(damaged)Cat")

    def test_saige_json_rejects_conflicting_ids_for_the_same_name(self):
        project = {"project": {"projectName": "ambiguous", "projectType": "det",
            "classInfos": [{"className": "Cat", "classId": 1},
                           {"className": "Cat", "classId": 2}],
            "projectFiles": []}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "ambiguous.json"
            path.write_text(json.dumps(project), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "同名类别.*classId"):
                load_saige_json(path)

    def test_saige_json_rejects_non_finite_numbers_in_unknown_fields(self):
        payload = """{"project":{"projectName":"bad","projectType":"det",
        "classInfos":[{"className":"ok"}],"projectFiles":[],"unknown":1e309}}"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "non-finite.json"
            path.write_text(payload, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "有限范围"):
                load_saige_json(path)

    def test_saige_json_rejects_duplicate_object_keys(self):
        payload = """{"project":{"projectName":"first","projectName":"second",
        "projectType":"det","classInfos":[],"projectFiles":[]}}"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "duplicate-key.json"
            path.write_text(payload, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复键.*projectName"):
                load_saige_json(path)

    def test_saige_json_rejects_non_finite_string_coordinates_and_bbox_overflow(self):
        for x, width, expected in (("NaN", 1, "有限数字"),
                                   ("1e308", "1e308", "有限范围")):
            with self.subTest(x=x, width=width), \
                    tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
                project = json.loads(json.dumps(PROJECT))
                label = project["project"]["projectFiles"][0]["labelDataList"][0]
                label.update(labelPosX=x, labelWidth=width)
                path = Path(directory) / "invalid-coordinate.json"
                path.write_text(json.dumps(project), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    load_saige_json(path)

    def test_saige_null_label_placeholder_keeps_the_original_index(self):
        project = json.loads(json.dumps(PROJECT))
        labels = project["project"]["projectFiles"][0]["labelDataList"]
        labels.insert(0, None)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "placeholder.json"
            path.write_text(json.dumps(project), encoding="utf-8")
            self.assertEqual(load_saige_json(path).items[0].metadata["label_no"], 1)

    def test_saige_rejects_non_object_project_files(self):
        project = {"project": {"projectName": "bad", "projectType": "det",
                               "classInfos": [], "projectFiles": [None]}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "bad-structure.json"
            path.write_text(json.dumps(project), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"projectFiles\[0\].*对象"):
                load_saige_json(path)

    def test_saige_only_synthesizes_image_level_items_for_image_level_projects(self):
        base = {"projectName": "fixture", "classInfos": [{"className": "ok"}],
                "projectFiles": [{"filePath": "a.jpg", "className": "ok",
                                  "labelDataList": []}]}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            for kind, expected in (("det", 0), ("iad", 1)):
                project = {"project": {**base, "projectType": kind}}
                path = root / f"{kind}.json"
                path.write_text(json.dumps(project), encoding="utf-8")
                self.assertEqual(len(load_saige_json(path).items), expected)

    def test_saige_object_labels_must_have_a_declared_class_name(self):
        project = {"project": {"projectName": "bad", "projectType": "det",
            "classInfos": [{"className": "ok"}],
            "projectFiles": [{"filePath": "a.jpg", "className": "image-summary",
                              "labelDataList": [{"labelId": 1}]}]}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "missing-class.json"
            path.write_text(json.dumps(project), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "对象标注缺少 className"):
                load_saige_json(path)

    def test_saige_rejects_one_class_id_shared_by_multiple_names(self):
        project = {"project": {"projectName": "bad", "projectType": "det",
            "classInfos": [{"className": "Cat", "classId": 1},
                           {"className": "Dog", "classId": 1}],
            "projectFiles": []}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "shared-id.json"
            path.write_text(json.dumps(project), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "同时属于"):
                load_saige_json(path)

    def test_srproj_rejects_non_finite_coordinates(self):
        xml = """<?xml version='1.0'?><Project><Type>Detection</Type><ClassGroup>
        <Class><Name>ok</Name></Class></ClassGroup><ImageGroup><Image><Path>a.jpg</Path>
        <LabelGroup><Label><ClassIndex>0</ClassIndex>
        <Coordinate X='NaN' Y='0' Width='1' Height='1'/></Label></LabelGroup>
        </Image></ImageGroup></Project>"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "non-finite.srproj"
            path.write_text(xml, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "有限数字"):
                load_srproj(path)

    def test_srproj_rejects_missing_or_out_of_range_label_class_indices(self):
        labels = (
            "<Label><ClassIndex>0</ClassIndex></Label>"
            "<Label><ClassIndex>9</ClassIndex></Label>"
        )
        missing = "<Label><Coordinate X='0' Y='0' Width='1' Height='1'/></Label>"
        for value, expected in ((labels, "越界类别索引"), (missing, "缺少有效")):
            with self.subTest(expected=expected), \
                    tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
                xml = ("<Project><Type>Detection</Type><ClassGroup>"
                       "<Class><Name>ok</Name></Class></ClassGroup><ImageGroup><Image>"
                       f"<Path>a.jpg</Path><LabelGroup>{value}</LabelGroup>"
                       "</Image></ImageGroup></Project>")
                path = Path(directory) / "invalid-index.srproj"
                path.write_text(xml, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    load_srproj(path)

    def test_folder_items_have_no_fabricated_analysis(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            (root / "ok").mkdir(parents=True)
            (root / "ok" / "a.jpg").write_bytes(b"fixture")
            item = load_folder(root).items[0]
            self.assertEqual((item.suggested_label, item.suspicion_score, item.analysis_state),
                             (None, None, "not_analyzed"))

    def test_folder_rejects_unlabelled_root_images(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            (root / "ok").mkdir(parents=True)
            (root / "ok" / "inside.jpg").write_bytes(b"inside")
            (root / "unlabelled.jpg").write_bytes(b"root")
            with self.assertRaisesRegex(ValueError, "根目录.*没有类别"):
                load_folder(root)

    def test_folder_rejects_links_before_loading(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            external = Path(directory) / "external"
            (root / "ok").mkdir(parents=True)
            external.mkdir()
            (external / "a.jpg").write_bytes(b"outside")
            link = root / "linked"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"当前环境不能创建目录符号链接：{error}")
            with self.assertRaisesRegex(ValueError, "符号链接|目录联接"):
                load_folder(root)

    def test_folder_link_rejection_is_covered_without_symlink_privileges(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            linked = root / "linked"
            linked.mkdir(parents=True)
            path_type = type(linked)
            real_is_symlink = path_type.is_symlink
            linked_resolved = linked.resolve()

            def identify_fixture_as_link(candidate):
                return candidate.resolve() == linked_resolved or real_is_symlink(candidate)

            with mock.patch.object(path_type, "is_symlink", identify_fixture_as_link):
                with self.assertRaisesRegex(ValueError, "符号链接|目录联接"):
                    load_folder(root)

    def test_folder_fingerprint_uses_content_not_only_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            (root / "ok").mkdir(parents=True)
            image = root / "ok" / "a.jpg"
            image.write_bytes(b"first")
            first = folder_fingerprint(root)
            stat = image.stat()
            image.write_bytes(b"other")
            os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertNotEqual(folder_fingerprint(root), first)

    def test_preview_cannot_escape_source_root(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            root = base / "dataset"
            (root / "ok").mkdir(parents=True)
            (root / "ok" / "a.jpg").write_bytes(b"inside")
            secret = base / "secret.jpg"
            secret.write_bytes(b"secret")
            dataset = load_folder(root)
            item = dataset.items[0]
            item.relative_path = str(Path("..") / "secret.jpg")
            with self.assertRaisesRegex(PermissionError, "未授权"):
                read_preview(dataset, item)
            item.relative_path = str(secret.resolve())
            with self.assertRaisesRegex(PermissionError, "未授权"):
                read_preview(dataset, item)

    def test_preview_allows_an_explicit_additional_root(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            root = base / "dataset"
            external = base / "external"
            (root / "ok").mkdir(parents=True)
            external.mkdir()
            (root / "ok" / "a.jpg").write_bytes(b"inside")
            image = external / "b.jpg"
            image.write_bytes(b"external")
            dataset = load_folder(root)
            dataset.metadata["allowed_preview_roots"] = [str(external)]
            dataset.items[0].relative_path = str(image.resolve())
            self.assertEqual(read_preview(dataset, dataset.items[0]), (b"external", "image/jpeg"))

    def test_visionproj_rejects_oversized_preview(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sample.json", json.dumps(PROJECT))
                archive.writestr("images/a.jpg", b"too-large")
            dataset = load_visionproj(path)
            with mock.patch("saige_reviewer.adapters.MAX_PREVIEW_BYTES", 4):
                self.assertIsNone(read_preview(dataset, dataset.items[0]))

    def test_visionproj_rejects_duplicate_archive_names(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "sample.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sample.json", json.dumps(PROJECT))
                with self.assertWarns(UserWarning):
                    archive.writestr("sample.json", json.dumps(PROJECT))
            with self.assertRaisesRegex(ValueError, "重复"):
                load_visionproj(path)

    def test_visionproj_rejects_nonempty_directory_entries(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "directory-payload.visionproj"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sample.json", json.dumps(PROJECT))
                archive.writestr("odd/", b"hidden-payload")
            with self.assertRaisesRegex(ValueError, "带数据的目录条目"):
                load_visionproj(path)

    def test_disk_preview_reads_with_a_hard_byte_limit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            image = root / "ok" / "a.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"x")
            dataset = load_folder(root)
            requested = []

            class GrowingStream:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, limit):
                    requested.append(limit)
                    return b"x" * limit

            path_type = type(image)
            real_open = path_type.open
            image_resolved = image.resolve()

            def controlled_open(candidate, *args, **kwargs):
                if candidate.resolve() == image_resolved:
                    return GrowingStream()
                return real_open(candidate, *args, **kwargs)

            with mock.patch.object(path_type, "open", controlled_open), \
                    mock.patch("saige_reviewer.adapters.MAX_PREVIEW_BYTES", 4):
                self.assertIsNone(read_preview(dataset, dataset.items[0]))
            self.assertEqual(requested, [5])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_original_preview_rejects_image_pixel_bombs_before_conversion(self):
        from PIL import Image

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            image_path = root / "ok" / "a.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (2, 2), "white").save(image_path)
            dataset = load_folder(root)
            with mock.patch("saige_reviewer.adapters.MAX_IMAGE_PIXELS", 3), \
                    mock.patch.object(Image.Image, "convert",
                                      side_effect=AssertionError("must not convert")):
                self.assertIsNone(render_preview(dataset, dataset.items[0], "original"))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_preview_draws_disconnected_contours_as_separate_closed_polygons(self):
        from PIL import Image

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            image_path = root / "ok" / "a.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (100, 100), "white").save(image_path)
            dataset = load_folder(root)
            item = dataset.items[0]
            item.metadata.update(
                bbox=[10, 10, 80, 80],
                contours=[
                    [[10, 10], [20, 10], [20, 20], [10, 20]],
                    [[70, 70], [80, 70], [80, 80], [70, 80]],
                ],
            )
            lines = []

            class RecordingDraw:
                def line(self, points, **_kwargs):
                    lines.append(points)

                def rectangle(self, *_args, **_kwargs):
                    raise AssertionError("polygon contours must not fall back to a rectangle")

            with mock.patch("PIL.ImageDraw.Draw", return_value=RecordingDraw()):
                self.assertIsNotNone(render_preview(dataset, item))
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(line[0] == line[-1] for line in lines))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_preview_can_crop_without_drawing_contours(self):
        from PIL import Image

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "dataset"
            image_path = root / "ok" / "a.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (100, 100), "white").save(image_path)
            dataset = load_folder(root)
            item = dataset.items[0]
            item.metadata.update(
                bbox=[40, 40, 60, 60],
                contours=[[[40, 40], [60, 40], [60, 60], [40, 60]]],
            )

            with mock.patch(
                    "PIL.ImageDraw.Draw",
                    side_effect=AssertionError("hidden contours must not be drawn")):
                preview = render_preview(dataset, item, show_contours=False)

            self.assertIsNotNone(preview)
            with Image.open(io.BytesIO(preview[0])) as rendered:
                self.assertEqual(rendered.size, (34, 34))


if __name__ == "__main__":
    unittest.main()
