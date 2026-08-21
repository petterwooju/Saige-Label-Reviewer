import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.remote_projects import RemoteProjectService, RemoteRuntimeConfig


PROJECT = {
    "project": {
        "projectName": "remote fixture",
        "projectType": "det",
        "classInfos": [
            {"className": "scratch", "classId": 1, "classColor": "#aa0000"},
            {"className": "dent", "classId": 2, "classColor": "#0000aa"},
        ],
        "projectFiles": [
            {
                "filePath": "images/a.jpg",
                "className": "image-summary",
                "labelDataList": [
                    {
                        "labelId": 7,
                        "className": "scratch",
                        "classId": 1,
                        "classColor": "#aa0000",
                        "labelPosX": 1,
                        "labelPosY": 2,
                        "labelWidth": 3,
                        "labelHeight": 4,
                    }
                ],
            }
        ],
    }
}


def vision_project_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("fixture.json", json.dumps(PROJECT))
        archive.writestr("images/a.jpg", b"image fixture")
    return stream.getvalue()


class RemoteProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        root = Path(self.temporary.name)
        config = RemoteRuntimeConfig(
            root=root,
            admin_emails=frozenset({"admin@saigeai.com"}),
            max_project_size=1024 * 1024,
            max_managed_storage=1024 * 1024 * 1024,
            min_free_space=0,
            retention_seconds=60,
            recycle_seconds=60,
            lease_seconds=30,
        )
        self.service = RemoteProjectService(config, start_workers=False)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def upload_vision_project(self, actor="member@saigeai.com"):
        payload = vision_project_bytes()
        upload = self.service.create_upload(
            actor,
            {
                "name": "fixture",
                "format": "visionproj",
                "total_size": len(payload),
                "file_count": 1,
                "primary_path": "project/fixture.visionproj",
            },
        )
        registered = self.service.register_files(
            actor,
            upload["id"],
            [
                {
                    "logical_path": "project/fixture.visionproj",
                    "role": "primary",
                    "size": len(payload),
                    "chunk_count": 1,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        )
        repeated = self.service.register_files(
            actor,
            upload["id"],
            [
                {
                    "logical_path": "project/fixture.visionproj",
                    "role": "primary",
                    "size": len(payload),
                    "chunk_count": 1,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        )
        self.assertEqual(repeated["files"], registered["files"])
        file_id = registered["files"][0]["file_id"]
        self.service.write_chunk(
            actor,
            upload["id"],
            file_id,
            0,
            payload,
            hashlib.sha256(payload).hexdigest(),
        )
        self.service.write_chunk(
            actor,
            upload["id"],
            file_id,
            0,
            payload,
            hashlib.sha256(payload).hexdigest(),
        )
        return self.service.complete_upload(actor, upload["id"])["project"]

    def upload_entries(self, project_format, primary_path, entries, actor="member@saigeai.com"):
        total = sum(len(payload) for _, payload, _ in entries)
        upload = self.service.create_upload(
            actor,
            {
                "name": f"fixture-{project_format}",
                "format": project_format,
                "total_size": total,
                "file_count": len(entries),
                "primary_path": primary_path,
            },
        )
        manifest = [
            {
                "logical_path": logical,
                "role": role,
                "size": len(payload),
                "chunk_count": 1,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for logical, payload, role in entries
        ]
        registered = self.service.register_files(actor, upload["id"], manifest)
        ids = {entry["logical_path"]: entry["file_id"] for entry in registered["files"]}
        for logical, payload, _ in entries:
            self.service.write_chunk(
                actor,
                upload["id"],
                ids[logical],
                0,
                payload,
                hashlib.sha256(payload).hexdigest(),
            )
        return self.service.complete_upload(actor, upload["id"])["project"]

    def test_upload_lock_revision_review_and_strict_export(self):
        actor = "member@saigeai.com"
        project = self.upload_vision_project(actor)
        self.assertEqual(self.service.list_projects("other@saigeai.com")[0]["id"], project["id"])
        initial = self.service.session_payload(actor, project["id"])
        self.assertTrue(initial["read_only"])
        compact = self.service.session_payload(actor, project["id"], compact=True)
        self.assertEqual((compact["item_count"], compact["items"]), (1, []))
        lease = self.service.acquire_lease(actor, project["id"])
        unlocked = self.service.session_payload(actor, project["id"], touch=False)
        self.assertFalse(unlocked["read_only"])
        patch = self.service.mutate(
            actor,
            project["id"],
            lease["token"],
            initial["project_revision"],
            "update",
            {"id": "0", "label": "dent", "status": "modified"},
        )
        self.assertEqual(patch["project_revision"], 1)
        page = self.service.list_items(
            actor, project["id"], label="dent", status="modified", limit=10
        )
        self.assertEqual((page["total"], page["items"][0]["id"]), (1, "0"))
        with self.assertRaisesRegex(RuntimeError, "版本已变化"):
            self.service.mutate(
                actor, project["id"], lease["token"], 0, "undo"
            )
        export = self.service.create_export("other@saigeai.com", project["id"])
        artifact, filename, _ = self.service.artifact(
            actor, project["id"], export["id"]
        )
        self.assertEqual(filename, "fixture.corrected.visionproj")
        with zipfile.ZipFile(artifact) as archive:
            manifest = json.loads(archive.read("fixture.json"))
        self.assertEqual(
            manifest["project"]["projectFiles"][0]["labelDataList"][0]["className"],
            "dent",
        )

    def test_edit_lease_is_exclusive_and_admin_can_force_takeover(self):
        project = self.upload_vision_project()
        first = self.service.acquire_lease("one@saigeai.com", project["id"])
        self.assertTrue(first["token"])
        with self.assertRaisesRegex(RuntimeError, "正在由"):
            self.service.acquire_lease("two@saigeai.com", project["id"])
        forced = self.service.acquire_lease(
            "admin@saigeai.com", project["id"], force=True
        )
        self.assertEqual(forced["holder"], "admin@saigeai.com")

    def test_srproj_json_and_folder_uploads_preserve_remote_relative_mapping(self):
        xml = b"""<?xml version='1.0'?><Project><Type>Detection</Type><ClassGroup>
        <Class><Name>scratch</Name></Class><Class><Name>dent</Name></Class></ClassGroup>
        <ImageGroup><Image><Path>a.jpg</Path><LabelGroup><Label><ClassIndex>0</ClassIndex>
        <Type>Box</Type><Coordinate X='1' Y='2' Width='3' Height='4'/>
        </Label></LabelGroup></Image></ImageGroup></Project>"""
        srproj = self.upload_entries(
            "srproj",
            "project/fixture.srproj",
            [
                ("project/fixture.srproj", xml, "primary"),
                ("roots/a.jpg", b"image-a", "data"),
            ],
        )
        saige_json = json.dumps(PROJECT).encode()
        project_json = self.upload_entries(
            "saige-json",
            "project/fixture.json",
            [
                ("project/fixture.json", saige_json, "primary"),
                ("roots/images/a.jpg", b"image-a", "data"),
            ],
        )
        folder = self.upload_entries(
            "folder",
            "dataset",
            [
                ("dataset/scratch/a.jpg", b"image-a", "data"),
                ("dataset/dent/b.jpg", b"image-b", "data"),
            ],
        )
        self.assertEqual(
            [srproj["format"], project_json["format"], folder["format"]],
            ["srproj", "saige-json", "folder"],
        )
        self.assertEqual(
            self.service.session_payload("member@saigeai.com", srproj["id"])["item_count"],
            1,
        )
        self.assertEqual(
            self.service.session_payload("member@saigeai.com", project_json["id"])["item_count"],
            1,
        )
        self.assertEqual(
            self.service.session_payload("member@saigeai.com", folder["id"])["item_count"],
            2,
        )
        export = self.service.create_export("member@saigeai.com", folder["id"])
        artifact, _, _ = self.service.artifact(
            "member@saigeai.com", folder["id"], export["id"]
        )
        with zipfile.ZipFile(artifact) as archive:
            self.assertIn("SAIGE_EXPORT_SHA256SUMS.txt", archive.namelist())
            self.assertIn("SAIGE_EXPORT_README.txt", archive.namelist())

    def test_admin_recycle_and_restore_preserves_project(self):
        project = self.upload_vision_project()
        with self.assertRaises(PermissionError):
            self.service.recycle_project("member@saigeai.com", project["id"])
        self.service.recycle_project("admin@saigeai.com", project["id"])
        self.assertEqual(self.service.list_projects("member@saigeai.com"), [])
        self.assertEqual(
            self.service.list_projects("admin@saigeai.com", recycled=True)[0]["id"],
            project["id"],
        )
        self.service.restore_project("admin@saigeai.com", project["id"])
        self.assertEqual(
            self.service.session_payload("member@saigeai.com", project["id"])["items"][0]["label"],
            "scratch",
        )

    def test_expiry_respects_active_lease_then_recycles_and_purges(self):
        project = self.upload_vision_project()
        lease = self.service.acquire_lease("member@saigeai.com", project["id"])
        with self.service._connect() as connection:
            connection.execute("UPDATE projects SET last_activity=0 WHERE id=?", (project["id"],))
        self.service.cleanup_expired()
        self.assertEqual(self.service.get_project("member@saigeai.com", project["id"])["state"], "active")
        self.service.release_lease("member@saigeai.com", project["id"], lease["token"])
        self.service.cleanup_expired()
        self.assertEqual(
            self.service.list_projects("admin@saigeai.com", recycled=True)[0]["id"],
            project["id"],
        )
        with self.service._connect() as connection:
            recycle_path = Path(
                connection.execute(
                    "SELECT recycle_path FROM projects WHERE id=?", (project["id"],)
                ).fetchone()[0]
            )
            connection.execute("UPDATE projects SET recycled_at=0 WHERE id=?", (project["id"],))
        self.assertTrue(recycle_path.is_dir())
        self.service.cleanup_expired()
        self.assertFalse(recycle_path.exists())
        with self.assertRaises(LookupError):
            self.service.get_project("member@saigeai.com", project["id"])

    def test_analysis_result_applies_atomically_and_advances_revision(self):
        project = self.upload_vision_project()
        lease = self.service.acquire_lease("member@saigeai.com", project["id"])

        def analyzer(dataset, config, cache_dir, progress):
            self.assertEqual(config.device, "auto")
            progress(0.5, "fixture")
            return {
                "item_updates": [
                    {
                        "id": item.id,
                        "x": 1.0,
                        "y": 2.0,
                        "suspicion_score": 80.0,
                        "suggested_label": "dent",
                        "label_confidence": 0.2,
                        "neighbor_support": 0.3,
                    }
                    for item in dataset.items
                ],
                "execution": {"device": "cpu", "dtype": "float32"},
            }

        self.service._analyzer = analyzer
        job = self.service.submit_analysis(
            "member@saigeai.com", project["id"], lease["token"], 0, {"device": "cuda"}
        )
        self.assertEqual(job["state"], "queued")
        self.service._run_analysis(job["id"])
        payload = self.service.session_payload(
            "member@saigeai.com", project["id"], touch=False
        )
        self.assertEqual(payload["project_revision"], 1)
        self.assertEqual(payload["items"][0]["analysis_state"], "analyzed")
        self.assertEqual(payload["job"]["result"]["execution"]["device"], "cpu")
        sample = self.service.feature_sample(
            "member@saigeai.com", project["id"], limit=20_000
        )
        self.assertEqual((sample["total"], sample["items"][0]["id"]), (1, "0"))

    def test_analysis_revision_conflict_does_not_leave_partial_scores(self):
        project = self.upload_vision_project()
        lease = self.service.acquire_lease("member@saigeai.com", project["id"])
        self.service._analyzer = lambda dataset, config, cache_dir, progress: {
            "item_updates": [
                {
                    "id": item.id,
                    "x": 1,
                    "y": 2,
                    "suspicion_score": 90,
                    "suggested_label": "dent",
                    "label_confidence": 0.1,
                    "neighbor_support": 0.1,
                }
                for item in dataset.items
            ]
        }
        job = self.service.submit_analysis(
            "member@saigeai.com", project["id"], lease["token"], 0, {}
        )
        with self.service._connect() as connection:
            connection.execute(
                "UPDATE projects SET revision=1 WHERE id=?", (project["id"],)
            )
        self.service._run_analysis(job["id"])
        payload = self.service.session_payload(
            "member@saigeai.com", project["id"], touch=False
        )
        self.assertEqual(payload["job"]["state"], "failed")
        self.assertEqual(payload["items"][0]["analysis_state"], "not_analyzed")
        self.assertIsNone(payload["items"][0]["suspicion_score"])


if __name__ == "__main__":
    unittest.main()
