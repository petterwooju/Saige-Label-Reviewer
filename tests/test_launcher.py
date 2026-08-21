import hashlib
import importlib
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1]


class LauncherTests(unittest.TestCase):
    def test_release_version_is_consistent_across_package_and_manifest(self):
        sys.path.insert(0, str(ROOT / "src"))
        from saige_reviewer import __version__

        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, "0.0.1")
        self.assertEqual(manifest["project"]["version"], __version__)
        self.assertIn("v0.0.1", (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0])
        index = (ROOT / "src" / "saige_reviewer" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(f">v{__version__}</span>", index)

    def test_failed_setup_exit_is_guarded_by_a_cmd_block(self):
        launcher = (ROOT / "Start Saige Reviewer.bat").read_text(encoding="utf-8")
        self.assertIn("if errorlevel 1 (", launcher)
        self.assertNotRegex(launcher, r"(?im)^if errorlevel \d+[^\r\n]*&[^\r\n]*exit")
        self.assertIn("goto launch", launcher)

    def test_incomplete_environment_requires_a_versioned_setup_marker(self):
        sys.path.insert(0, str(ROOT))
        with mock.patch.dict(os.environ, {"SAIGE_REVIEWER_NO_VENV": "1"}):
            run = importlib.import_module("run")

        supported = SimpleNamespace(major=3, minor=12)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                mock.patch.object(run, "ROOT", Path(directory)), \
                mock.patch.object(run.sys, "version_info", supported), \
                mock.patch.object(run.platform, "python_implementation", return_value="CPython"), \
                mock.patch.object(run.platform, "python_version", return_value="3.12.9"):
            ready, message = run._setup_ready()
            self.assertFalse(ready)
            self.assertIn("安装", message)

    def test_setup_invalidates_marker_and_uses_an_exclusive_lock(self):
        setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("[System.IO.FileShare]::None", setup)
        self.assertLess(setup.index("Remove-Item -LiteralPath $setupMarker"),
                        setup.index("python.exe failed to create .venv"))
        self.assertIn("model_weight_sha256", setup)
        self.assertIn("model_config_sha256", setup)

    def test_setup_probes_missing_python_and_torch_without_terminating(self):
        setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("$selectorProbeExitCode", setup)
        self.assertIn("$ErrorActionPreference = 'SilentlyContinue'", setup)
        self.assertIn("except Exception:", setup)
        self.assertIn("print('no')", setup)
        self.assertIn("allow_patterns=['config.json', 'model.safetensors']", setup)
        self.assertIn('$torchReady = & $venvPython -c $torchCheck $expectedTorchFlavor', setup)
        self.assertIn("python_implementation", setup)
        self.assertIn("python_version", setup)
        self.assertGreater(setup.index("--check-setup"), setup.index("Move-Item -LiteralPath $markerTemporary"))
        post_check = setup.index("--check-setup")
        cleanup = setup.index("Remove-Item -LiteralPath $setupMarker", post_check)
        self.assertGreater(cleanup, post_check)
        self.assertIn("if (-not $postCheckSucceeded)", setup[post_check:cleanup + 200])

    def test_complete_marker_for_another_python_identity_is_rejected(self):
        sys.path.insert(0, str(ROOT))
        with mock.patch.dict(os.environ, {"SAIGE_REVIEWER_NO_VENV": "1"}):
            run = importlib.import_module("run")
        from saige_reviewer import __version__
        from saige_reviewer.analysis import AnalysisConfig

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            manifest = b"[project]\nname='identity-test'\n"
            (root / "pyproject.toml").write_bytes(manifest)
            marker_path = root / ".venv" / ".saige-reviewer-setup.json"
            marker_path.parent.mkdir()
            marker = {
                "schema": 1,
                "app_version": __version__,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "model_revision": AnalysisConfig().model_revision,
                "model_download_skipped": True,
                "model_weight_sha256": None,
                "model_weight_size": None,
                "model_config_sha256": None,
                "model_config_size": None,
                "torch_variant": "CPU",
                "python_implementation": "CPython",
                "python_version": "3.12.9",
            }
            supported = SimpleNamespace(major=3, minor=12)
            with mock.patch.object(run, "ROOT", root), \
                    mock.patch.object(run.sys, "version_info", supported), \
                    mock.patch.object(run.platform, "python_implementation", return_value="CPython"), \
                    mock.patch.object(run.platform, "python_version", return_value="3.12.9"):
                for key, wrong in (("python_implementation", "PyPy"),
                                   ("python_version", "3.12.0-other")):
                    with self.subTest(key=key):
                        payload = {**marker, key: wrong}
                        marker_path.write_text(json.dumps(payload), encoding="utf-8")
                        ready, _ = run._setup_ready()
                        self.assertFalse(ready)

    def test_setup_ready_requires_cpython_312_or_313_runtime(self):
        sys.path.insert(0, str(ROOT))
        with mock.patch.dict(os.environ, {"SAIGE_REVIEWER_NO_VENV": "1"}):
            run = importlib.import_module("run")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                mock.patch.object(run, "ROOT", Path(directory)):
            with mock.patch.object(run.platform, "python_implementation", return_value="PyPy"):
                ready, message = run._setup_ready()
                self.assertFalse(ready)
                self.assertIn("CPython", message)
            unsupported = SimpleNamespace(major=3, minor=11)
            with mock.patch.object(run.sys, "version_info", unsupported):
                ready, message = run._setup_ready()
                self.assertFalse(ready)
                self.assertIn("3.12", message)


if __name__ == "__main__":
    unittest.main()
