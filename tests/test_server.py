import errno
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from types import ModuleType, SimpleNamespace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from saige_reviewer.demo import make_demo
from saige_reviewer import __version__
from saige_reviewer.server import (AppState, Job, PathPickerError, STATIC_TYPES,
                                   _PATH_PICKER_LOCK, _acquire_instance_lock,
                                   _release_instance_lock, create_handler,
                                   pick_local_path, run,
                                   ThreadingHTTPServer as ReviewerThreadingHTTPServer)
from saige_reviewer.session import ReviewSession


class ServerTests(unittest.TestCase):
    def test_javascript_has_executable_mime_type(self):
        self.assertEqual(STATIC_TYPES[".js"], "text/javascript; charset=utf-8")

    def test_reviewer_server_never_shares_an_occupied_port(self):
        self.assertFalse(ReviewerThreadingHTTPServer.allow_reuse_address)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            first = ReviewerThreadingHTTPServer(("127.0.0.1", 0), create_handler(
                AppState(workspace=root / "first", start_empty=True), "first-token"
            ))
            try:
                with self.assertRaises(OSError):
                    ReviewerThreadingHTTPServer(first.server_address, create_handler(
                        AppState(workspace=root / "second", start_empty=True), "second-token"
                    ))
            finally:
                first.server_close()

    def test_stale_preview_frontend_refreshes_session_and_uses_persistent_warning(self):
        script = (Path(__file__).parents[1] / "src" / "saige_reviewer" / "static" /
                  "app.js").read_text(encoding="utf-8")
        preview_error = next(
            line for line in script.splitlines()
            if line.startswith("$('#preview img').addEventListener('error'")
        )
        self.assertIn("response.status===409&&detail.analysis_stale", preview_error)
        self.assertIn("diagnosticUrl.searchParams.set('diagnose','1')", preview_error)
        self.assertIn("installState(await fetchSession());renderFilters();render()", preview_error)
        self.assertIn("notify(detail.error", preview_error)
        self.assertIn(",true,true)", preview_error)
        self.assertIn("请刷新页面", preview_error)

    def test_overwrite_frontend_preserves_result_when_session_refresh_fails(self):
        script = (Path(__file__).parents[1] / "src" / "saige_reviewer" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        do_export = next(line for line in script.splitlines()
                         if line.startswith("async function doExport"))
        self.assertIn("result=value.export_result", do_export)
        self.assertIn("if(value.reload_required)state.requires_source_reload=true", do_export)
        self.assertIn("try{installState(await fetchSession())", do_export)
        self.assertIn("catch(error){refreshError=error;state.requires_source_reload=true}", do_export)
        self.assertIn("result.recovery_copy", do_export)
        self.assertIn("persistent=Boolean(result.recovery_copy||value.reload_error||refreshError)",
                      do_export)
        self.assertIn(",persistent);render()", do_export)
        self.assertIn("if(!persistent)toastTimer=setTimeout", script)
        self.assertIn("toast.dataset.persistent==='true'", script)
        self.assertIn("toast.dataset.persistent='false'", script)
        self.assertIn("$('#toast').addEventListener('click',closeToast)", script)

    def test_http_server_version_matches_package_version(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            handler = create_handler(state, "token")
            self.assertEqual(handler.server_version, f"SaigeReviewer/{__version__}")
            self.assertEqual(state.payload()["app_version"], __version__)

        static = Path(__file__).parents[1] / "src" / "saige_reviewer" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="appVersion"', html)
        self.assertIn("state.app_version", script)

    def test_product_start_state_is_empty_until_a_source_is_opened(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory), start_empty=True)
            payload = state.payload()
            self.assertEqual(payload["name"], "尚未打开数据源")
            self.assertEqual(payload["source_type"], "empty")
            self.assertIsNone(payload["source"])
            self.assertEqual(payload["items"], [])
            self.assertEqual(payload["classes"], [])
            self.assertFalse(any(item.get("analysis_state") == "demo"
                                 for item in payload["items"]))

    def test_workspace_instance_lock_rejects_a_second_process_slot(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            first = _acquire_instance_lock(Path(directory))
            try:
                with self.assertRaises(RuntimeError):
                    _acquire_instance_lock(Path(directory))
            finally:
                _release_instance_lock(first)
            second = _acquire_instance_lock(Path(directory))
            _release_instance_lock(second)

    def _request(self, state, method, path, body=None, token="test-token", return_headers=False,
                 handler_token=None, host=None):
        expected_token = token if handler_token is None else handler_token
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(state, expected_token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            encoded = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"X-Review-Token": token}
            if host is not None:
                headers["Host"] = host
            if encoded is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            response_headers = dict(response.getheaders())
            value = json.loads(response.read().decode("utf-8"))
            connection.close()
            if return_headers:
                return response.status, value, response_headers
            return response.status, value
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_job_endpoint_is_lightweight_and_never_contains_dataset(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.job = Job(id="job", state="completed", result={"effective_input_size": 224})
            status, value = self._request(state, "GET", "/api/job")
            self.assertEqual(status, 200)
            self.assertEqual(set(value), {"job"})
            self.assertNotIn("items", json.dumps(value))
            self.assertNotIn("classes", value)

    def test_responses_prevent_embedding_and_restrict_document_capabilities(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            status, _, headers = self._request(
                state, "GET", "/api/session", return_headers=True
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            policy = headers["Content-Security-Policy"]
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertIn("base-uri 'none'", policy)
            self.assertIn("form-action 'self'", policy)

    def test_update_endpoint_returns_only_item_patch_and_summary(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            status, value = self._request(
                state, "POST", "/api/update", {"id": "0", "status": "correct"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(value["item"]["status"], "correct")
            self.assertTrue(value["can_undo"])
            self.assertNotIn("items", value)
            self.assertNotIn("classes", value)
            self.assertNotIn("name", value)

    def test_non_object_json_request_is_rejected_as_json(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            status, value = self._request(state, "POST", "/api/update", [])
            self.assertEqual(status, 400)
            self.assertIn("error", value)

    def test_native_picker_uses_file_and_folder_dialogs_and_normalizes_results(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root_path = Path(directory)
            selected_file = root_path / "dataset.json"
            selected_file.write_text("{}", encoding="utf-8")
            selected_folder = root_path / "dataset"
            selected_folder.mkdir()

            window = SimpleNamespace(
                withdraw=Mock(), attributes=Mock(), update_idletasks=Mock(), destroy=Mock()
            )
            dialog = ModuleType("tkinter.filedialog")
            dialog.askopenfilename = Mock(return_value=str(selected_file))
            dialog.askdirectory = Mock(return_value=str(selected_folder))
            tkinter = ModuleType("tkinter")
            tkinter.TclError = RuntimeError
            tkinter.Tk = Mock(return_value=window)
            tkinter.filedialog = dialog

            with patch.dict(sys.modules, {"tkinter": tkinter, "tkinter.filedialog": dialog}):
                self.assertEqual(pick_local_path("file"), str(selected_file.resolve()))
                self.assertEqual(pick_local_path("folder"), str(selected_folder.resolve()))

            dialog.askopenfilename.assert_called_once()
            dialog.askdirectory.assert_called_once()
            self.assertEqual(window.destroy.call_count, 2)

    def test_native_picker_failure_is_wrapped_without_exposing_platform_details(self):
        tkinter = ModuleType("tkinter")
        tkinter.TclError = RuntimeError
        tkinter.Tk = Mock(side_effect=OSError("private platform failure"))
        dialog = ModuleType("tkinter.filedialog")
        tkinter.filedialog = dialog
        with patch.dict(sys.modules, {"tkinter": tkinter, "tkinter.filedialog": dialog}):
            with self.assertRaisesRegex(PathPickerError, "无法打开本地资源管理器") as raised:
                pick_local_path("file")
        self.assertNotIn("private platform failure", str(raised.exception))

    def test_native_picker_rejects_a_second_concurrent_dialog(self):
        self.assertTrue(_PATH_PICKER_LOCK.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(PathPickerError, "已有一个文件选择窗口"):
                pick_local_path("folder")
        finally:
            _PATH_PICKER_LOCK.release()

    def test_pick_path_api_supports_file_and_folder_without_opening_a_session(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            selected_file = root / "dataset.json"
            selected_file.write_text("{}", encoding="utf-8")
            selected_folder = root / "dataset"
            selected_folder.mkdir()
            state = AppState(workspace=root / "workspace")
            original_session = state.session
            original_generation = state._session_generation

            for kind, selected in (("file", selected_file), ("folder", selected_folder)):
                with self.subTest(kind=kind), \
                        patch("saige_reviewer.server.pick_local_path",
                              return_value=str(selected.parent / "." / selected.name)) as picker:
                    status, value = self._request(
                        state, "POST", "/api/pick-path", {"kind": kind}
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(value, {
                        "path": str(selected.resolve()), "cancelled": False
                    })
                    picker.assert_called_once_with(kind, "source")

            self.assertIs(state.session, original_session)
            self.assertEqual(state._session_generation, original_generation)
            self.assertEqual(state.job.state, "idle")
            self.assertEqual(state.recent(), [])

    def test_pick_path_api_returns_empty_path_when_selection_is_cancelled(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch("saige_reviewer.server.pick_local_path", return_value=""):
                status, value = self._request(
                    state, "POST", "/api/pick-path", {"kind": "file"}
                )
            self.assertEqual(status, 200)
            self.assertEqual(value, {"path": "", "cancelled": True})

    def test_pick_path_api_rejects_invalid_kind_without_opening_picker(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch("saige_reviewer.server.pick_local_path") as picker:
                for body in ({}, {"kind": "directory"}, {"kind": 1}):
                    with self.subTest(body=body):
                        status, value = self._request(state, "POST", "/api/pick-path", body)
                        self.assertEqual(status, 400)
                        self.assertIn("kind", value["error"])
                picker.assert_not_called()

    def test_pick_path_api_uses_controlled_preview_root_purpose(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            selected = root / "external-images"
            selected.mkdir()
            state = AppState(workspace=root / "workspace")
            with patch("saige_reviewer.server.pick_local_path",
                       return_value=str(selected)) as picker:
                status, value = self._request(
                    state, "POST", "/api/pick-path",
                    {"kind": "folder", "purpose": "preview_root"}
                )
            self.assertEqual(status, 200)
            self.assertEqual(value, {
                "path": str(selected.resolve()), "cancelled": False
            })
            picker.assert_called_once_with("folder", "preview_root")

    def test_pick_path_api_rejects_invalid_or_mismatched_purpose(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch("saige_reviewer.server.pick_local_path") as picker:
                for body in (
                    {"kind": "folder", "purpose": "arbitrary"},
                    {"kind": "file", "purpose": "preview_root"},
                    {"kind": "folder", "purpose": 1},
                ):
                    with self.subTest(body=body):
                        status, value = self._request(
                            state, "POST", "/api/pick-path", body
                        )
                        self.assertEqual(status, 400)
                        self.assertTrue(
                            "purpose" in value["error"] or
                            "外部图像目录" in value["error"]
                        )
                picker.assert_not_called()

    def test_pick_path_api_returns_controlled_json_when_picker_fails(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch("saige_reviewer.server.pick_local_path",
                       side_effect=PathPickerError("无法打开本地资源管理器")):
                status, value = self._request(
                    state, "POST", "/api/pick-path", {"kind": "folder"}
                )
            self.assertEqual(status, 503)
            self.assertEqual(value, {"error": "无法打开本地资源管理器"})

    def test_pick_path_api_keeps_existing_host_and_token_protection(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch("saige_reviewer.server.pick_local_path") as picker:
                status, value = self._request(
                    state, "POST", "/api/pick-path", {"kind": "file"},
                    token="wrong", handler_token="expected"
                )
                self.assertEqual(status, 403)
                self.assertEqual(value, {"error": "Forbidden"})
                status, value = self._request(
                    state, "POST", "/api/pick-path", {"kind": "file"},
                    token="expected", handler_token="expected", host="example.invalid"
                )
                self.assertEqual(status, 403)
                self.assertEqual(value, {"error": "Forbidden"})
                picker.assert_not_called()

    def test_open_validates_and_remembers_explicit_preview_roots(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "dataset"
            (source / "ok").mkdir(parents=True)
            (source / "ok" / "a.jpg").write_bytes(b"fixture")
            external = root / "external-images"
            external.mkdir()
            state = AppState(workspace=root / "workspace")
            state.open(str(source), [str(external)])
            self.assertEqual(state.session.dataset.metadata["allowed_preview_roots"],
                             [str(external.resolve())])
            self.assertEqual(state.payload()["allowed_preview_roots"], [str(external.resolve())])
            self.assertEqual(state.recent()[0]["allowed_preview_roots"], [str(external.resolve())])
            with self.assertRaises(ValueError):
                state.open(str(source), ["relative/path"])
            with self.assertRaises(ValueError):
                state.open(str(source), [str(root / "missing")])
            with self.assertRaises(ValueError):
                state.open(str(source), [str(external)] * 9)

    def test_preview_root_can_be_authorized_without_reopening_the_session(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "dataset"
            (source / "ok").mkdir(parents=True)
            (source / "ok" / "a.jpg").write_bytes(b"fixture")
            external = root / "external-images"
            external.mkdir()
            state = AppState(source=source, workspace=root / "workspace")
            original_session = state.session

            status, value = self._request(
                state, "POST", "/api/authorize-preview-root", {"path": str(external)}
            )

            self.assertEqual(status, 200)
            self.assertIs(state.session, original_session)
            self.assertEqual(value["allowed_preview_roots"], [str(external.resolve())])
            self.assertEqual(state.recent()[0]["allowed_preview_roots"],
                             [str(external.resolve())])

    def test_preview_permission_error_returns_json_403(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch("saige_reviewer.server.render_preview",
                       side_effect=PermissionError("图像路径不在已授权根目录中")):
                status, value = self._request(state, "GET", "/api/preview/0")
            self.assertEqual(status, 403)
            self.assertIn("已授权", value["error"])

    def test_preview_contours_query_is_forwarded_to_renderer(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            with patch(
                    "saige_reviewer.server.render_preview",
                    return_value=(b"{}", "application/json")) as renderer:
                status, value = self._request(
                    state, "GET", "/api/preview/0?view=crop&contours=0"
                )

            self.assertEqual(status, 200)
            self.assertEqual(value, {})
            renderer.assert_called_once_with(
                state.session.dataset, state.session.dataset.items[0], "crop",
                show_contours=False,
            )

    def test_changed_external_preview_clears_all_analysis_and_returns_conflict(self):
        project = {"project": {
            "projectName": "stale-preview",
            "projectType": "det",
            "classInfos": [{"className": "ok"}],
            "projectFiles": [{
                "filePath": "a.jpg",
                "labelDataList": [{"className": "ok"}],
            }],
        }}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "project.json"
            image = root / "a.jpg"
            source.write_text(json.dumps(project), encoding="utf-8")
            image.write_bytes(b"first")
            state = AppState(workspace=root / "workspace")
            state.open(str(source))
            updates = self._updates(state.session.dataset)
            updates[0]["_analysis_preview_sha256"] = hashlib.sha256(b"first").hexdigest()
            state.session.apply_analysis_updates(updates)
            state.job = Job(state="completed", result={"effective_projection": "umap"})
            image.write_bytes(b"changed")

            status, value = self._request(state, "GET", "/api/preview/0")

            self.assertEqual(status, 409)
            self.assertTrue(value["analysis_stale"])
            self.assertIn("重新运行分析", value["error"])
            diagnostic_status, diagnostic = self._request(
                state, "GET", "/api/preview/0?diagnose=1"
            )
            self.assertEqual(diagnostic_status, 409)
            self.assertTrue(diagnostic["analysis_stale"])
            item = state.session.dataset.items[0]
            self.assertEqual(item.analysis_state, "not_analyzed")
            self.assertIsNone(item.suspicion_score)
            self.assertIsNone(item.suggested_label)
            self.assertNotIn("_analysis_preview_sha256", item.metadata)
            self.assertEqual(state.job.state, "idle")
            self.assertIsNone(state.job.result)

    def test_running_analysis_blocks_mutations_with_conflict(self):
        entered, release = threading.Event(), threading.Event()

        def waiting_analysis(dataset, config, cache_dir, progress):
            entered.set()
            release.wait(3)
            return {"item_updates": self._updates(dataset), "effective_input_size": 224}

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            with patch("saige_reviewer.server.analyze", side_effect=waiting_analysis):
                state.start_analysis({})
                self.assertTrue(entered.wait(2))
                status, value = self._request(
                    state, "POST", "/api/update", {"id": "0", "status": "correct"}
                )
                self.assertEqual(status, 409)
                self.assertIn("分析正在运行", value["error"])
                release.set()
                state._analysis_thread.join(timeout=3)

    def test_thread_start_failure_does_not_leave_analysis_running(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            with patch("saige_reviewer.server.threading.Thread.start",
                       side_effect=RuntimeError("cannot start thread")):
                with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                    state.start_analysis({})
            self.assertEqual(state.job.state, "failed")
            self.assertIn("cannot start thread", state.job.error)
            self.assertIsNone(state._analysis_thread)
            # A failed launch must not keep all subsequent actions behind a 409.
            status, value = self._request(
                state, "POST", "/api/update", {"id": "0", "status": "correct"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(value["item"]["status"], "correct")

    def test_thread_constructor_failure_does_not_leave_analysis_running(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            with patch("saige_reviewer.server.threading.Thread",
                       side_effect=RuntimeError("cannot construct thread")):
                with self.assertRaisesRegex(RuntimeError, "cannot construct thread"):
                    state.start_analysis({})
            self.assertEqual(state.job.state, "failed")
            self.assertIn("cannot construct thread", state.job.error)
            self.assertIsNone(state._analysis_thread)
            state.ensure_idle()

    def test_occupied_fixed_port_falls_back_and_reports_actual_port(self):
        addresses = []

        class FakeServer:
            server_address = ("127.0.0.1", 43210)

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        def bind(address, _handler):
            addresses.append(address)
            if len(addresses) == 1:
                raise OSError(10048, "address already in use")
            return FakeServer()

        dataset = SimpleNamespace(name="fixture", items=[])
        fake_state = SimpleNamespace(session=SimpleNamespace(dataset=dataset))
        lock = object()
        with patch("saige_reviewer.server._acquire_instance_lock", return_value=lock), \
                patch("saige_reviewer.server._release_instance_lock") as release, \
                patch("saige_reviewer.server.AppState", return_value=fake_state), \
                patch("saige_reviewer.server.ThreadingHTTPServer", side_effect=bind), \
                patch("builtins.print") as output:
            run(None, "127.0.0.1", 8765)
        self.assertEqual(addresses, [("127.0.0.1", 8765), ("127.0.0.1", 0)])
        self.assertTrue(any("43210" in str(call) for call in output.call_args_list))
        release.assert_called_once_with(lock)

    def test_non_address_bind_error_is_not_hidden_by_port_fallback(self):
        lock = object()
        dataset = SimpleNamespace(name="fixture", items=[])
        fake_state = SimpleNamespace(session=SimpleNamespace(dataset=dataset))
        denied = OSError(errno.EACCES, "permission denied")
        with patch("saige_reviewer.server._acquire_instance_lock", return_value=lock), \
                patch("saige_reviewer.server._release_instance_lock") as release, \
                patch("saige_reviewer.server.AppState", return_value=fake_state), \
                patch("saige_reviewer.server.ThreadingHTTPServer", side_effect=denied) as bind:
            with self.assertRaises(OSError) as raised:
                run(None, "127.0.0.1", 8765)
        self.assertIs(raised.exception, denied)
        bind.assert_called_once()
        release.assert_called_once_with(lock)

    @staticmethod
    def _updates(dataset):
        return [{
            "id": item.id,
            "x": float(index),
            "y": float(index + 1),
            "suspicion_score": 12.5,
            "suggested_label": item.label,
            "label_confidence": 0.875,
            "neighbor_support": 0.8,
            "analysis_state": "analyzed",
        } for index, item in enumerate(dataset.items)]

    def test_analysis_applies_atomically_and_strips_private_updates_from_job(self):
        def completed_analysis(dataset, config, cache_dir, progress):
            return {
                "item_updates": self._updates(dataset),
                "effective_input_size": 224,
                "config": {"projection": "umap"},
            }

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            with patch("saige_reviewer.server.analyze", side_effect=completed_analysis):
                state.start_analysis({})
                state._analysis_thread.join(timeout=3)
            self.assertEqual(state.job.state, "completed")
            self.assertTrue(all(item.analysis_state == "analyzed" for item in state.session.dataset.items))
            serialized = json.dumps(state.job_payload())
            self.assertNotIn("item_updates", serialized)
            self.assertNotIn('"items"', serialized)

    def test_stale_analysis_cannot_pollute_a_replaced_session(self):
        entered, release = threading.Event(), threading.Event()

        def delayed_analysis(dataset, config, cache_dir, progress):
            entered.set()
            release.wait(3)
            return {"item_updates": self._updates(dataset), "effective_input_size": 224}

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            with patch("saige_reviewer.server.analyze", side_effect=delayed_analysis):
                state.start_analysis({})
                self.assertTrue(entered.wait(2))
                replacement = ReviewSession(make_demo())
                replacement.dataset.name = "replacement"
                with state.lock:
                    state.session = replacement
                    state._session_generation += 1
                    state.job = Job()
                release.set()
                state._analysis_thread.join(timeout=3)
            self.assertIs(state.session, replacement)
            self.assertEqual(state.session.dataset.name, "replacement")
            self.assertEqual(state.job.state, "idle")
            self.assertTrue(all("label_confidence" not in item.metadata for item in replacement.dataset.items))

    def test_update_save_failure_rolls_back_item_summary_and_history(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            item = state.session.dataset.items[0]
            with patch.object(state.session, "save", side_effect=OSError("disk full")):
                status, value = self._request(
                    state, "POST", "/api/update", {"id": item.id, "status": "correct"}
                )
            self.assertEqual(status, 400)
            self.assertIn("error", value)
            self.assertEqual(item.status, "pending")
            self.assertFalse(state.session.undo_stack)
            self.assertFalse(state.session.redo_stack)
            self.assertEqual(state.session.summary_payload()["status_counts"]["pending"],
                             len(state.session.dataset.items))

    def test_undo_save_failure_rolls_back_and_retry_undoes_exactly_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            item = state.session.dataset.items[0]
            state.session.update(item.id, status="correct")
            state.save_session()
            with patch.object(state.session, "save", side_effect=OSError("disk full")):
                status, _ = self._request(state, "POST", "/api/undo", {})
            self.assertEqual(status, 400)
            self.assertEqual(item.status, "correct")
            self.assertTrue(state.session.undo_stack)
            self.assertFalse(state.session.redo_stack)
            status, value = self._request(state, "POST", "/api/undo", {})
            self.assertEqual(status, 200)
            self.assertEqual(value["item"]["status"], "pending")
            self.assertFalse(state.session.undo_stack)
            status, value = self._request(state, "POST", "/api/undo", {})
            self.assertEqual(status, 200)
            self.assertIsNone(value["item"])
            self.assertEqual(item.status, "pending")

    def test_analysis_runtime_outputs_are_not_written_to_human_session_state(self):
        def completed_analysis(dataset, config, cache_dir, progress):
            return {"item_updates": self._updates(dataset), "effective_input_size": 224}

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = AppState(workspace=Path(directory))
            state.session.dataset.source = Path(directory) / "fixture.json"
            before = [(item.x, item.y, item.suspicion_score, item.suggested_label,
                       item.analysis_state, dict(item.metadata)) for item in state.session.dataset.items]
            with patch("saige_reviewer.server.analyze", side_effect=completed_analysis), \
                    patch.object(state.session, "save", side_effect=OSError("must not be called")) as save:
                state.start_analysis({})
                state._analysis_thread.join(timeout=3)
            after = [(item.x, item.y, item.suspicion_score, item.suggested_label,
                      item.analysis_state, dict(item.metadata)) for item in state.session.dataset.items]
            self.assertEqual(state.job.state, "completed")
            self.assertNotEqual(after, before)
            save.assert_not_called()

    def test_successful_overwrite_returns_recovery_info_even_if_reload_fails(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            state = AppState(workspace=root / "workspace")
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            image_root = root / "images"
            image_root.mkdir()
            state.session.dataset.source = source
            state.session.dataset.metadata["allowed_preview_roots"] = [str(image_root.resolve())]
            exported = {
                "output": str(source),
                "backup": str(root / "backup.json"),
                "recovery_copy": str(root / "recovery.json"),
            }
            with patch("saige_reviewer.server.export_corrected", return_value=exported), \
                    patch.object(state, "open", side_effect=ValueError("reload boom")) as reopen:
                status, value = self._request(
                    state, "POST", "/api/overwrite", {"confirmation": "OVERWRITE_SOURCE"}
                )
            self.assertEqual(status, 200, value)
            self.assertEqual(value["export_result"]["recovery_copy"], exported["recovery_copy"])
            self.assertTrue(value["reload_required"])
            self.assertIn("reload boom", value["reload_error"])
            reopen.assert_called_once_with(str(source), [str(image_root.resolve())])
            self.assertTrue(state.requires_source_reload)
            status, value = self._request(
                state, "POST", "/api/update", {"id": "0", "status": "correct"}
            )
            self.assertEqual(status, 409)
            self.assertIn("重新打开", value["error"])

    def test_overwrite_reload_failure_restores_stale_guard_even_after_open_clears_it(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            state = AppState(workspace=root / "workspace")
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            state.session.dataset.source = source

            def partially_opened(*_args, **_kwargs):
                state.requires_source_reload = False
                raise ValueError("restore failed after replacement")

            exported = {"output": str(source), "recovery_copy": str(root / "recovery.json")}
            with patch("saige_reviewer.server.export_corrected", return_value=exported), \
                    patch.object(state, "open", side_effect=partially_opened):
                status, value = self._request(
                    state, "POST", "/api/overwrite", {"confirmation": "OVERWRITE_SOURCE"}
                )
            self.assertEqual(status, 200, value)
            self.assertIn("restore failed", value["reload_error"])
            self.assertTrue(state.requires_source_reload)
            status, _ = self._request(
                state, "POST", "/api/update", {"id": "0", "status": "correct"}
            )
            self.assertEqual(status, 409)

    def test_identical_sources_at_different_paths_keep_separate_review_state(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            first = root / "dataset-a"
            second = root / "dataset-b"
            for source in (first, second):
                (source / "ok").mkdir(parents=True)
                (source / "ok" / "a.jpg").write_bytes(b"identical fixture")

            state = AppState(workspace=root / "workspace")
            state.open(str(first))
            first_hash = state.session.dataset.source_hash
            first_state_path = state._state_path()
            state.session.update("0", status="correct")
            state.save_session()

            state.open(str(second))
            self.assertEqual(state.session.dataset.source_hash, first_hash)
            second_state_path = state._state_path()
            self.assertNotEqual(first_state_path, second_state_path)
            state.session.update("0", status="uncertain")
            state.save_session()

            state.open(str(first))
            self.assertEqual(state.session.dataset.items[0].status, "correct")
            state.open(str(second))
            self.assertEqual(state.session.dataset.items[0].status, "uncertain")

    def test_legacy_hash_named_v2_state_migrates_only_to_its_exact_source(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            first = root / "dataset-a"
            second = root / "dataset-b"
            for source in (first, second):
                (source / "ok").mkdir(parents=True)
                (source / "ok" / "a.jpg").write_bytes(b"identical fixture")
            workspace = root / "workspace"

            original = AppState(first, workspace=workspace)
            original.session.update("0", status="correct")
            legacy = workspace / "sessions" / f"{original.session.dataset.source_hash}.json"
            original.session.save(legacy)
            migrated_path = original._state_path()
            self.assertFalse(migrated_path.exists())

            migrated = AppState(first, workspace=workspace)
            self.assertEqual(migrated.session.dataset.items[0].status, "correct")
            self.assertTrue(migrated_path.exists())
            self.assertTrue(legacy.exists())

            unrelated = AppState(second, workspace=workspace)
            self.assertEqual(unrelated.session.dataset.source_hash,
                             migrated.session.dataset.source_hash)
            self.assertEqual(unrelated.session.dataset.items[0].status, "pending")
            self.assertFalse(unrelated._state_path().exists())

    def test_valid_legacy_state_recovers_a_corrupt_new_state_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "dataset"
            (source / "ok").mkdir(parents=True)
            (source / "ok" / "a.jpg").write_bytes(b"fixture")
            workspace = root / "workspace"
            original = AppState(source, workspace=workspace)
            original.session.update("0", status="correct")
            legacy = original._legacy_state_path()
            original.session.save(legacy)
            target = original._state_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{broken", encoding="utf-8")

            recovered = AppState(source, workspace=workspace)
            self.assertEqual(recovered.session.dataset.items[0].status, "correct")
            self.assertTrue(legacy.exists())
            self.assertTrue(recovered.session.restore(target))

    def test_successful_overwrite_reloads_with_existing_preview_authorizations(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "dataset"
            (source / "ok").mkdir(parents=True)
            (source / "ok" / "a.jpg").write_bytes(b"fixture")
            image_root = root / "external-images"
            image_root.mkdir()
            state = AppState(workspace=root / "workspace")
            state.session.dataset.source = source
            state.session.dataset.metadata["allowed_preview_roots"] = [str(image_root.resolve())]
            exported = {"output": str(source), "backup": str(root / "backup")}
            with patch("saige_reviewer.server.export_corrected", return_value=exported):
                status, value = self._request(
                    state, "POST", "/api/overwrite", {"confirmation": "OVERWRITE_SOURCE"}
                )
            self.assertEqual(status, 200)
            self.assertNotIn("reload_error", value)
            self.assertTrue(value["reload_required"])
            self.assertFalse(state.requires_source_reload)
            self.assertEqual(state.session.dataset.metadata["allowed_preview_roots"],
                             [str(image_root.resolve())])
            self.assertEqual(state.recent()[0]["allowed_preview_roots"], [str(image_root.resolve())])


if __name__ == "__main__":
    unittest.main()
