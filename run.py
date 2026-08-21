"""Repository-local launcher; keeps the MVP install-free."""

from __future__ import annotations

import sys
import os
import hashlib
import importlib
import importlib.metadata
import json
import platform
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve() and not os.environ.get("SAIGE_REVIEWER_NO_VENV"):
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(ROOT / "src"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _setup_ready() -> tuple[bool, str]:
    marker_path = ROOT / ".venv" / ".saige-reviewer-setup.json"
    try:
        python_implementation = platform.python_implementation()
        python_version = platform.python_version()
        if (python_implementation != "CPython" or sys.version_info.major != 3 or
                sys.version_info.minor not in {12, 13}):
            return False, "分析环境必须使用 CPython 3.12 或 3.13"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        manifest_hash = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()
        from saige_reviewer import __version__
        from saige_reviewer.analysis import AnalysisConfig, dependency_status, model_status

        config = AnalysisConfig()
        if (not isinstance(marker, dict) or marker.get("schema") != 1 or
                marker.get("app_version") != __version__ or
                marker.get("manifest_sha256") != manifest_hash or
                marker.get("model_revision") != config.model_revision or
                marker.get("python_implementation") != python_implementation or
                marker.get("python_version") != python_version or
                not isinstance(marker.get("model_download_skipped"), bool)):
            return False, "安装标记与当前版本不一致"
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        requirements = manifest["project"]["optional-dependencies"]["analysis"]
        for requirement in requirements:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", requirement)
            if not match:
                return False, f"无法核验非精确依赖：{requirement}"
            distribution, expected = match.groups()
            try:
                installed = importlib.metadata.version(distribution).split("+", 1)[0]
            except importlib.metadata.PackageNotFoundError:
                return False, f"缺少固定依赖：{distribution}"
            if installed != expected:
                return False, f"依赖版本不匹配：{distribution} {installed}（需要 {expected}）"
        dependencies = dependency_status()
        if not dependencies["ready"]:
            return False, dependencies["message"]
        for module in ("numpy", "PIL", "sklearn", "torch", "transformers", "umap"):
            try:
                importlib.import_module(module)
            except Exception as error:
                return False, f"依赖无法导入：{module}（{error}）"
        import torch
        torch_variant = marker.get("torch_variant")
        if torch_variant == "CPU":
            if getattr(torch.version, "cuda", None) is not None:
                return False, "安装标记要求 CPU PyTorch，但当前是 CUDA 构建"
        elif torch_variant == "CUDA 13.0":
            if str(getattr(torch.version, "cuda", "")) != "13.0" or not torch.cuda.is_available():
                return False, "CUDA 13.0 PyTorch 或 GPU 当前不可用"
        else:
            return False, "安装标记中的 PyTorch 类型无效"
        if not marker["model_download_skipped"]:
            model = model_status(config)
            if not model["cached"]:
                return False, model["message"]
            from huggingface_hub import try_to_load_from_cache

            weights = try_to_load_from_cache(
                config.model, "model.safetensors", revision=config.model_revision
            )
            model_config = try_to_load_from_cache(
                config.model, "config.json", revision=config.model_revision
            )
            weight_path = Path(weights) if isinstance(weights, str) else None
            config_path = Path(model_config) if isinstance(model_config, str) else None
            expected_size = marker.get("model_weight_size")
            expected_hash = marker.get("model_weight_sha256")
            if (weight_path is None or not weight_path.is_file() or
                    not isinstance(expected_size, int) or weight_path.stat().st_size != expected_size or
                    not isinstance(expected_hash, str) or
                    _file_sha256(weight_path) != expected_hash):
                return False, "固定模型权重完整性校验失败"
            if (config_path is None or not config_path.is_file() or
                    not isinstance(marker.get("model_config_size"), int) or
                    config_path.stat().st_size != marker["model_config_size"] or
                    not isinstance(marker.get("model_config_sha256"), str) or
                    _file_sha256(config_path) != marker["model_config_sha256"]):
                return False, "固定模型配置完整性校验失败"
        return True, "分析环境已就绪"
    except (ImportError, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return False, f"安装尚未完成：{error}"


if sys.argv[1:] == ["--check-setup"]:
    ready, message = _setup_ready()
    print(message)
    raise SystemExit(0 if ready else 1)

from saige_reviewer.server import main


if __name__ == "__main__":
    main()
