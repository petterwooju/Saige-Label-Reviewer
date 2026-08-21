import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.adapters import load_source, source_fingerprint
from saige_reviewer.exporter import export_corrected
from saige_reviewer.session import ReviewSession


XML = """<?xml version='1.0'?><Project><Type>Detection</Type><ClassGroup>
<Class><Name>ok</Name></Class><Class><Name>ng</Name></Class></ClassGroup><ImageGroup><Image>
<Path>a.jpg</Path><LabelGroup><Label><ClassIndex>0</ClassIndex><Type>Box</Type>
<Coordinate X='1' Y='2' Width='3' Height='4'/></Label></LabelGroup></Image></ImageGroup></Project>"""

SAIGE_DETECTION = {"project": {"projectName": "fixture", "projectType": "det",
    "classInfos": [
        {"className": "scratch", "classId": 1, "classColor": "#aa0000"},
        {"className": "dent", "classId": 2, "classColor": "#0000aa"},
    ],
    "projectFiles": [{"filePath": "images/a.jpg", "className": "image-summary",
        "classId": 99, "labelDataList": [{"labelId": 7, "className": "scratch",
            "classId": 1, "classColor": "#aa0000", "labelPosX": 1, "labelPosY": 2,
            "labelWidth": 3, "labelHeight": 4}]}]}}


class ExporterTests(unittest.TestCase):
    def _session(self, directory: Path):
        source = directory / "fixture.srproj"
        source.write_text(XML, encoding="utf-8")
        session = ReviewSession(load_source(source))
        session.update("0", label="ng", status="modified")
        return source, session

    def _folder_session(self, directory: Path, *, same_name: bool = False):
        source = directory / "dataset"
        (source / "ok").mkdir(parents=True)
        (source / "ng").mkdir()
        (source / "ok" / "a.jpg").write_bytes(b"image-a")
        name = "a.jpg" if same_name else "b.jpg"
        (source / "ng" / name).write_bytes(b"image-b")
        (source / "notes.txt").write_text("preserve me", encoding="utf-8")
        session = ReviewSession(load_source(source))
        item = next(item for item in session.dataset.items if Path(item.relative_path).parts[0] == "ok")
        session.update(item.id, label="ng", status="modified")
        return source, session

    def _saige_session(self, directory: Path, *, archive: bool = False):
        source = directory / ("fixture.visionproj" if archive else "fixture.json")
        if archive:
            with zipfile.ZipFile(source, "w") as output:
                output.writestr("fixture.json", json.dumps(SAIGE_DETECTION))
                output.writestr("images/", b"")
                output.writestr("images/a.jpg", b"image")
        else:
            source.write_text(json.dumps(SAIGE_DETECTION), encoding="utf-8")
        session = ReviewSession(load_source(source))
        session.update("0", label="dent", status="modified")
        return source, session

    @staticmethod
    def _files(root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_default_export_preserves_source_and_verifies_backup(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._session(root)
            before = source_fingerprint(source)
            result = export_corrected(session, root / "workspace")
            self.assertEqual(source_fingerprint(source), before)
            self.assertEqual(source_fingerprint(Path(result["backup"])), before)
            self.assertEqual(Path(result["backup"]).suffix, ".srproj")
            self.assertEqual(load_source(Path(result["backup"])).items[0].label, "ok")
            self.assertEqual(load_source(Path(result["output"])).items[0].label, "ng")

    def test_srproj_export_preserves_comments_processing_instructions_and_unknown_nodes(self):
        decorated = XML.replace(
            "<?xml version='1.0'?><Project>",
            "<?xml version='1.0'?><!-- before root --><?saige keep=\"before\"?>"
            "<!DOCTYPE Project><Project>",
        ).replace(
            "<Project>",
            "<Project><?saige keep=\"yes\"?><!-- critical operator note -->"
            "<UnknownSetting enabled=\"true\">keep me</UnknownSetting>",
        ).replace("</Project>", "</Project><!-- after root -->")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source = root / "decorated.srproj"
            source.write_text(decorated, encoding="utf-8")
            session = ReviewSession(load_source(source))
            session.update("0", label="ng", status="modified")
            result = export_corrected(session, root / "workspace")
            output = Path(result["output"]).read_text(encoding="utf-8")
            self.assertEqual(output, decorated.replace("<ClassIndex>0", "<ClassIndex>1"))
            self.assertIn("<!-- before root -->", output)
            self.assertIn('<?saige keep="before"?>', output)
            self.assertIn("<!DOCTYPE Project>", output)
            self.assertIn("<!-- after root -->", output)
            self.assertIn('<?saige keep="yes"?>', output)
            self.assertIn("<!-- critical operator note -->", output)
            self.assertIn('<UnknownSetting enabled="true">keep me</UnknownSetting>', output)

    def test_srproj_export_patches_mixed_content_index_without_moving_lexical_nodes(self):
        decorated = XML.replace(
            "<ClassIndex>0</ClassIndex>",
            "<ClassIndex><!-- keep before -->0<?saige keep-after?></ClassIndex>",
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source = root / "mixed.srproj"
            source.write_text(decorated, encoding="utf-8")
            session = ReviewSession(load_source(source))
            session.update("0", label="ng", status="modified")
            result = export_corrected(session, root / "workspace")
            output = Path(result["output"]).read_text(encoding="utf-8")
            self.assertEqual(
                output,
                decorated.replace(
                    "<ClassIndex><!-- keep before -->0<?saige keep-after?>",
                    "<ClassIndex><!-- keep before -->1<?saige keep-after?>",
                ),
            )
            self.assertEqual(load_source(Path(result["output"])).items[0].label, "ng")

    def test_srproj_many_replacements_handle_growing_numeric_widths_in_one_pass(self):
        classes = "".join(f"<Class><Name>c{index}</Name></Class>" for index in range(101))
        labels = "".join(
            f"<Label><ClassIndex>{index}</ClassIndex><Type>Box</Type>"
            "<Coordinate X='1' Y='2' Width='3' Height='4'/></Label>"
            for index in (9, 99)
        )
        project = (
            "<?xml version='1.0'?><Project><Type>Detection</Type>"
            f"<ClassGroup>{classes}</ClassGroup><ImageGroup><Image><Path>a.jpg</Path>"
            f"<LabelGroup>{labels}</LabelGroup></Image></ImageGroup></Project>"
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source = root / "widths.srproj"
            source.write_text(project, encoding="utf-8")
            session = ReviewSession(load_source(source))
            session.update("0", label="c10", status="modified")
            session.update("1", label="c100", status="modified")
            result = export_corrected(session, root / "workspace")
            output = Path(result["output"]).read_text(encoding="utf-8")
            expected = project.replace(
                "<ClassIndex>9</ClassIndex>", "<ClassIndex>10</ClassIndex>"
            ).replace(
                "<ClassIndex>99</ClassIndex>", "<ClassIndex>100</ClassIndex>"
            )
            self.assertEqual(output, expected)
            self.assertEqual(
                [item.label for item in load_source(Path(result["output"])).items],
                ["c10", "c100"],
            )

    def test_external_change_blocks_export(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._session(root)
            source.write_text(XML.replace("</Project>", "<OtherSettings/></Project>"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "外部修改"):
                export_corrected(session, root / "workspace")

    def test_saige_detection_updates_object_identity_not_file_class(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            _, session = self._saige_session(root)
            result = export_corrected(session, root / "workspace")
            data = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            file = data["project"]["projectFiles"][0]
            label = file["labelDataList"][0]
            self.assertEqual((label["className"], label["classId"], label["classColor"]),
                             ("dent", 2, "#0000aa"))
            self.assertEqual((file["className"], file["classId"]), ("image-summary", 99))

    def test_visionproj_uses_the_same_consistent_class_writeback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            _, session = self._saige_session(root, archive=True)
            result = export_corrected(session, root / "workspace")
            with zipfile.ZipFile(result["output"]) as archive:
                data = json.loads(archive.read("fixture.json").decode("utf-8"))
            label = data["project"]["projectFiles"][0]["labelDataList"][0]
            self.assertEqual((label["className"], label["classId"], label["classColor"]),
                             ("dent", 2, "#0000aa"))

    def test_visionproj_rejects_a_truncated_copied_entry_before_overwrite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._saige_session(root, archive=True)

            def truncate_copy(_source, target, **_kwargs):
                target.write(b"truncated")

            with patch("saige_reviewer.exporter.shutil.copyfileobj", side_effect=truncate_copy):
                with self.assertRaisesRegex(RuntimeError, "归档条目内容"):
                    export_corrected(session, root / "workspace", overwrite=True)

            with zipfile.ZipFile(source) as archive:
                self.assertEqual(archive.read("images/a.jpg"), b"image")
            self.assertEqual(load_source(source).items[0].label, "scratch")
            self.assertFalse(any(".tmp" in path.name for path in root.iterdir()))

    def test_saige_image_level_class_updates_file_and_removes_stale_id(self):
        project = {"project": {"projectName": "iad", "projectType": "iad",
            "classInfos": [{"className": "ok", "classId": 1}, {"className": "ng"}],
            "projectFiles": [{"filePath": "images/a.jpg", "className": "ok", "classId": 1,
                              "labelDataList": []}]}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source = root / "iad.json"
            source.write_text(json.dumps(project), encoding="utf-8")
            session = ReviewSession(load_source(source))
            session.update("0", label="ng", status="modified")
            result = export_corrected(session, root / "workspace")
            file = json.loads(Path(result["output"]).read_text(encoding="utf-8"))[
                "project"]["projectFiles"][0]
            self.assertEqual(file["className"], "ng")
            self.assertNotIn("classId", file)

    def test_saige_writeback_preserves_null_placeholder_indices(self):
        project = json.loads(json.dumps(SAIGE_DETECTION))
        project["project"]["projectFiles"][0]["labelDataList"].insert(0, None)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source = root / "placeholder.json"
            source.write_text(json.dumps(project), encoding="utf-8")
            session = ReviewSession(load_source(source))
            self.assertEqual(session.dataset.items[0].metadata["label_no"], 1)
            session.update("0", label="dent", status="modified")
            result = export_corrected(session, root / "workspace")
            labels = json.loads(Path(result["output"]).read_text(encoding="utf-8"))["project"][
                "projectFiles"][0]["labelDataList"]
            self.assertIsNone(labels[0])
            self.assertEqual((labels[1]["className"], labels[1]["classId"]), ("dent", 2))

    def test_explicit_overwrite_uses_verified_backup(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._session(root)
            before = source_fingerprint(source)
            result = export_corrected(session, root / "workspace", overwrite=True)
            self.assertTrue(result["overwritten"])
            self.assertEqual(source_fingerprint(Path(result["backup"])), before)
            self.assertEqual(load_source(source).items[0].label, "ng")
            recovery = Path(result["recovery_copy"])
            self.assertTrue(recovery.exists())
            self.assertEqual(load_source(recovery).items[0].label, "ok")

    @unittest.skipUnless(os.name == "nt", "Windows no-clobber rename fallback")
    def test_file_overwrite_falls_back_safely_when_hard_links_are_unsupported(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._session(root)
            with patch("saige_reviewer.exporter.os.link",
                       side_effect=OSError("hard links unsupported")):
                result = export_corrected(session, root / "workspace", overwrite=True)
            self.assertEqual(load_source(source).items[0].label, "ng")
            self.assertEqual(load_source(Path(result["recovery_copy"])).items[0].label, "ok")

    def test_folder_overwrite_is_validated_then_swapped(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            original = self._files(source)
            result = export_corrected(session, root / "workspace", overwrite=True)

            self.assertFalse((source / "ok" / "a.jpg").exists())
            self.assertEqual((source / "ng" / "a.jpg").read_bytes(), b"image-a")
            self.assertEqual((source / "ng" / "b.jpg").read_bytes(), b"image-b")
            self.assertEqual((source / "notes.txt").read_text(encoding="utf-8"), "preserve me")
            self.assertEqual(self._files(Path(result["backup"])), original)
            self.assertEqual(source_fingerprint(source), result["output_hash"])

    def test_folder_validation_failure_leaves_original_untouched(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            original = self._files(source)
            with patch("saige_reviewer.exporter._validate", side_effect=RuntimeError("injected validation failure")):
                with self.assertRaisesRegex(RuntimeError, "validation failure"):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertEqual(self._files(source), original)
            self.assertTrue((source / "ok" / "a.jpg").is_file())

    def test_folder_move_failure_only_damages_disposable_shadow(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            original = self._files(source)
            with patch("saige_reviewer.exporter.shutil.move", side_effect=OSError("injected move failure")):
                with self.assertRaisesRegex(OSError, "move failure"):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertEqual(self._files(source), original)
            self.assertFalse(any("saige-staging" in path.name for path in source.parent.iterdir()))

    def test_failed_directory_swap_restores_original_path(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            original = self._files(source)
            real_rename = os.rename
            source_resolved = source.resolve()
            def fail_validated_swap(src, dst):
                if (Path(dst).resolve(strict=False) == source_resolved and
                        "saige-staging" in Path(src).name):
                    raise OSError("injected swap failure")
                return real_rename(src, dst)

            with patch("saige_reviewer.exporter.os.rename", side_effect=fail_validated_swap):
                with self.assertRaisesRegex(OSError, "swap failure"):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertTrue(source.is_dir())
            self.assertEqual(self._files(source), original)

    def test_change_after_last_precheck_is_preserved_and_blocks_swap(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            from saige_reviewer import exporter

            real_check = exporter._check_source_unchanged
            checks = 0

            def mutate_after_precheck(*args, **kwargs):
                nonlocal checks
                result = real_check(*args, **kwargs)
                checks += 1
                if checks == 2:
                    (source / "late-external.txt").write_text("must survive", encoding="utf-8")
                return result

            with patch("saige_reviewer.exporter._check_source_unchanged",
                       side_effect=mutate_after_precheck):
                with self.assertRaisesRegex(RuntimeError, "外部修改"):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertEqual((source / "late-external.txt").read_text(encoding="utf-8"),
                             "must survive")
            self.assertTrue((source / "ok" / "a.jpg").is_file())

    def test_source_recreated_during_commit_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            real_rename = os.rename
            source_resolved = source.resolve()

            def recreate_after_displace(src, dst):
                result = real_rename(src, dst)
                if Path(src).resolve(strict=False) == source_resolved:
                    source.mkdir()
                    (source / "external.txt").write_text("new owner", encoding="utf-8")
                return result

            with patch("saige_reviewer.exporter.os.rename", side_effect=recreate_after_displace):
                with self.assertRaisesRegex(RuntimeError, "重新创建"):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertEqual((source / "external.txt").read_text(encoding="utf-8"), "new owner")
            recoveries = list(root.glob(".dataset.original-*"))
            self.assertEqual(len(recoveries), 1)
            self.assertTrue((recoveries[0] / "ok" / "a.jpg").is_file())

    def test_backup_and_export_names_survive_same_second_and_uuid_collision(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            fixed_uuid = SimpleNamespace(hex="0" * 32)
            with (patch("saige_reviewer.exporter._stamp", return_value="20260807_120000"),
                  patch("saige_reviewer.exporter.uuid.uuid4", return_value=fixed_uuid)):
                first = export_corrected(session, root / "workspace")
                second = export_corrected(session, root / "workspace")
            self.assertNotEqual(first["backup"], second["backup"])
            self.assertNotEqual(first["output"], second["output"])
            self.assertTrue(Path(first["backup"]).exists())
            self.assertTrue(Path(second["backup"]).exists())

    def test_directory_backup_never_merges_a_concurrently_inserted_child(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            from saige_reviewer import exporter

            real_copy = exporter._copy_directory_exclusive
            injected = False

            def inject_child(copy_source, destination, *, root_created=False):
                nonlocal injected
                if root_created and not injected:
                    injected = True
                    # An active process could put a junction here.  A regular
                    # pre-existing directory exercises the same fail-closed
                    # exclusive-creation branch without requiring privileges.
                    (destination / "ok").mkdir()
                return real_copy(copy_source, destination, root_created=root_created)

            with patch(
                "saige_reviewer.exporter._copy_directory_exclusive",
                side_effect=inject_child,
            ):
                with self.assertRaises(FileExistsError):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertTrue((source / "ok" / "a.jpg").is_file())

    def test_external_change_during_staging_aborts_before_swap(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            real_copytree = __import__("shutil").copytree
            source_resolved = source.resolve()

            def mutate_after_staging(*args, **kwargs):
                result = real_copytree(*args, **kwargs)
                destination = Path(args[1])
                if (Path(args[0]).resolve() == source_resolved and
                        "saige-staging" in destination.name):
                    (source / "external.txt").write_text("external change", encoding="utf-8")
                return result

            with patch("saige_reviewer.exporter.shutil.copytree", side_effect=mutate_after_staging):
                with self.assertRaisesRegex(RuntimeError, "外部修改"):
                    export_corrected(session, root / "workspace", overwrite=True)
            self.assertTrue((source / "ok" / "a.jpg").is_file())
            self.assertEqual((source / "external.txt").read_text(encoding="utf-8"), "external change")

    def test_path_escape_and_target_collision_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root)
            item = next(item for item in session.dataset.items if item.original_label == "ok")
            item.relative_path = str(Path("..") / "escape.jpg")
            with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                export_corrected(session, root / "workspace", overwrite=True)
            self.assertTrue((source / "ok" / "a.jpg").is_file())

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            root = Path(value)
            source, session = self._folder_session(root, same_name=True)
            with self.assertRaisesRegex(RuntimeError, "同一目标路径"):
                export_corrected(session, root / "workspace", overwrite=True)
            self.assertTrue((source / "ok" / "a.jpg").is_file())
            self.assertTrue((source / "ng" / "a.jpg").is_file())

    def test_workspace_inside_source_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as value:
            source, session = self._folder_session(Path(value))
            with self.assertRaisesRegex(ValueError, "工作目录"):
                export_corrected(session, source / ".saige-workspace", overwrite=True)
            self.assertFalse((source / ".saige-workspace").exists())


if __name__ == "__main__":
    unittest.main()
