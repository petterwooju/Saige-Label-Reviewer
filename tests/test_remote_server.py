import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.remote_server import (
    RemoteJobManager,
    RemoteServerConfig,
    ThreadingHTTPServer,
    create_remote_handler,
)


PROJECT = {
    "project": {
        "projectName": "remote fixture",
        "projectType": "det",
        "classInfos": [{"className": "cat"}, {"className": "dog"}],
        "projectFiles": [
            {
                "filePath": "images/a.jpg",
                "labelDataList": [
                    {
                        "labelId": 1,
                        "className": "cat",
                        "labelPosX": 1,
                        "labelPosY": 2,
                        "labelWidth": 3,
                        "labelHeight": 4,
                    }
                ],
            },
            {
                "filePath": "images/b.jpg",
                "labelDataList": [
                    {
                        "labelId": 2,
                        "className": "dog",
                        "labelPosX": 2,
                        "labelPosY": 3,
                        "labelWidth": 4,
                        "labelHeight": 5,
                    }
                ],
            },
        ],
    }
}


def make_visionproj(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps(PROJECT))
        archive.writestr("images/a.jpg", b"jpeg-a")
        archive.writestr("images/b.jpg", b"jpeg-b")
    return path.read_bytes()


def fake_analyze(dataset, config, cache_dir, progress):
    progress(0.5, "fake GPU analysis")
    updates = []
    for index, item in enumerate(dataset.items):
        updates.append(
            {
                "id": item.id,
                "x": float(index),
                "y": float(index + 1),
                "suspicion_score": 20.0 + index * 60,
                "suggested_label": "dog" if item.original_label == "cat" else "cat",
                "label_confidence": 0.2 + index * 0.1,
                "neighbor_support": 0.3 + index * 0.1,
            }
        )
    progress(1.0, "done")
    return {
        "item_updates": updates,
        "effective_input_size": 224,
        "effective_projection": "svd",
        "cache_hit": False,
        "algorithm_version": "fixture-v1",
        "execution": {"device": "cuda", "dtype": "float16"},
    }


class RemoteManagerTests(unittest.TestCase):
    owner = "reviewer@saigeai.com"

    def _upload(self, manager: RemoteJobManager, payload: bytes, filename="sample.visionproj"):
        count = (len(payload) + manager.chunk_size - 1) // manager.chunk_size
        job = manager.create_upload(self.owner, filename, len(payload), count)
        for index in range(count):
            chunk = payload[index * manager.chunk_size:(index + 1) * manager.chunk_size]
            manager.write_chunk(
                self.owner, job["id"], index, chunk, hashlib.sha256(chunk).hexdigest()
            )
        return job

    def test_chunked_upload_runs_one_worker_and_returns_paginated_results(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            payload = make_visionproj(root / "source.visionproj")
            manager = RemoteJobManager(
                root / "remote", chunk_size=37, analyzer=fake_analyze, start_worker=True
            )
            try:
                created = self._upload(manager, payload)
                manager.complete_upload(
                    self.owner, created["id"], hashlib.sha256(payload).hexdigest()
                )
                manager._queue.join()
                job = manager.get_job(self.owner, created["id"])
                self.assertEqual(job["state"], "completed")
                self.assertEqual(job["summary"]["device"], "cuda")
                value = manager.query_results(self.owner, created["id"], 1, 1)
                self.assertEqual(len(value["items"]), 1)
                self.assertEqual(value["items"][0]["suspicion_score"], 80.0)
                self.assertNotIn(str(root), json.dumps(value))
                preview = manager.preview(self.owner, created["id"], value["items"][0]["id"], "crop", True)
                self.assertIsNotNone(preview)
                self.assertEqual(preview[1], "image/jpeg")
            finally:
                manager.close()

    def test_owner_isolation_and_one_active_job_limit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            manager = RemoteJobManager(Path(directory), chunk_size=8, start_worker=False)
            try:
                job = manager.create_upload(self.owner, "one.visionproj", 9, 2)
                with self.assertRaisesRegex(RuntimeError, "already has an active"):
                    manager.create_upload(self.owner, "two.visionproj", 8, 1)
                with self.assertRaises(LookupError):
                    manager.get_job("other@saigeai.com", job["id"])
                self.assertEqual(manager.list_jobs("other@saigeai.com"), [])
            finally:
                manager.close()

    def test_rejects_paths_oversize_chunks_duplicate_chunks_and_wrong_digest(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            manager = RemoteJobManager(
                Path(directory), chunk_size=4, max_upload_size=8, start_worker=False
            )
            try:
                for name in ("../x.visionproj", "x.json", "C:\\x.visionproj"):
                    with self.assertRaises(ValueError):
                        manager.create_upload(self.owner, name, 4, 1)
                with self.assertRaises(ValueError):
                    manager.create_upload(self.owner, "x.visionproj", 9, 3)
                job = manager.create_upload(self.owner, "ok.visionproj", 5, 2)
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    manager.write_chunk(self.owner, job["id"], 0, b"abcd", "0" * 64)
                digest = hashlib.sha256(b"abcd").hexdigest()
                manager.write_chunk(self.owner, job["id"], 0, b"abcd", digest)
                with self.assertRaisesRegex(RuntimeError, "already uploaded"):
                    manager.write_chunk(self.owner, job["id"], 0, b"abcd", digest)
                with self.assertRaisesRegex(ValueError, "declared position"):
                    manager.write_chunk(
                        self.owner, job["id"], 1, b"zz", hashlib.sha256(b"zz").hexdigest()
                    )
            finally:
                manager.close()

    def test_invalid_archive_fails_closed_and_releases_active_slot(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            manager = RemoteJobManager(Path(directory), chunk_size=4, start_worker=False)
            try:
                created = self._upload(manager, b"not-a-zip")
                with self.assertRaises(Exception):
                    manager.complete_upload(self.owner, created["id"])
                self.assertEqual(manager.get_job(self.owner, created["id"])["state"], "failed")
                manager.create_upload(self.owner, "next.visionproj", 4, 1)
            finally:
                manager.close()

    def test_expired_nonrunning_jobs_are_removed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            manager = RemoteJobManager(
                Path(directory), chunk_size=4, retention_seconds=10, start_worker=False
            )
            try:
                job = manager.create_upload(self.owner, "old.visionproj", 4, 1)
                internal = manager._jobs[job["id"]]
                internal.updated_at = 1.0
                manager._save(internal)
                self.assertEqual(manager.cleanup_expired(now=20.0), [job["id"]])
                self.assertFalse((manager.jobs_root / job["id"]).exists())
            finally:
                manager.close()


class RemoteHTTPTests(unittest.TestCase):
    hostname = "saige-label-reviewer-beta.saigeai.com"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.manager = RemoteJobManager(Path(self.temporary.name), start_worker=False)

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def _request(self, path, *, email=None, assertion=True, host=None, method="GET", body=None):
        config = RemoteServerConfig(self.hostname, "saigeai.com")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_remote_handler(self.manager, config)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            headers = {"Host": host or self.hostname}
            if email is not None:
                headers["Cf-Access-Authenticated-User-Email"] = email
            if assertion:
                headers["Cf-Access-Jwt-Assertion"] = "verified-by-cloudflared-connector"
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            value = response.read()
            result_headers = dict(response.getheaders())
            connection.close()
            return response.status, value, result_headers
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_requires_verified_access_assertion_and_exact_allowed_domain(self):
        status, _, _ = self._request("/api/me", email="person@saigeai.com", assertion=False)
        self.assertEqual(status, 403)
        status, _, _ = self._request("/api/me", email="person@evilsaigeai.com")
        self.assertEqual(status, 403)
        status, payload, _ = self._request("/api/me", email="PERSON@SAIGEAI.COM")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["email"], "person@saigeai.com")

    def test_rejects_wrong_host_and_does_not_expose_local_path_api(self):
        status, _, _ = self._request("/", email="person@saigeai.com", host="localhost")
        self.assertEqual(status, 403)
        status, _, _ = self._request(
            "/api/open", email="person@saigeai.com", method="POST", body={"source": "C:\\"}
        )
        self.assertEqual(status, 404)

    def test_static_page_has_security_headers(self):
        status, payload, headers = self._request("/", email="person@saigeai.com")
        self.assertEqual(status, 200)
        self.assertIn(b"Saige Label Reviewer", payload)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_explicit_dev_identity_only_works_on_loopback_host(self):
        config = RemoteServerConfig(self.hostname, "saigeai.com", "dev@saigeai.com")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_remote_handler(self.manager, config)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", "/api/me", headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["email"], "dev@saigeai.com")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
