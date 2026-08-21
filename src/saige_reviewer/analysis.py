from __future__ import annotations

import ast
import hashlib
import hmac
import importlib
import importlib.metadata
import importlib.util
import inspect
import io
import json
import math
import os
import posixpath
import re
import struct
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import __version__ as APP_VERSION
from .adapters import MAX_IMAGE_PIXELS, read_preview, source_fingerprint
from .domain import Dataset

Progress = Callable[[float, str], None]
REQUIRED_MODULES = ("numpy", "PIL", "sklearn", "torch", "transformers")
DEPENDENCY_DISTRIBUTIONS = {
    "numpy": "numpy",
    "Pillow": "Pillow",
    "scikit-learn": "scikit-learn",
    "scipy": "scipy",
    "joblib": "joblib",
    "threadpoolctl": "threadpoolctl",
    "torch": "torch",
    "transformers": "transformers",
    "tokenizers": "tokenizers",
    "safetensors": "safetensors",
    "huggingface-hub": "huggingface-hub",
    "umap-learn": "umap-learn",
    "numba": "numba",
    "llvmlite": "llvmlite",
    "pynndescent": "pynndescent",
}

CACHE_SCHEMA_VERSION = 2
ANALYSIS_ALGORITHM_VERSION = "dinov2-label-review-v3"
DEFAULT_MODEL_REVISION = "f9e44c814b77203eaa57a6bdbbd535f21ede1415"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAX_FEATURE_MATRIX_BYTES = 512 * 1024 * 1024
MAX_SCORING_WORKING_BYTES = 512 * 1024 * 1024
MAX_FINITE_CHECK_ELEMENTS = 1024 * 1024
MAX_TSNE_ITEMS = 50_000
APPROXIMATE_NEIGHBOR_THRESHOLD = 10_000


@dataclass(slots=True)
class AnalysisConfig:
    model: str = "facebook/dinov2-base"
    model_revision: str = DEFAULT_MODEL_REVISION
    model_access: str = "local_only"
    background_mode: str = "original"
    input_size: str = "auto"
    roi_expansion: float = 0.15
    mask_pooling: bool = True
    projection: str = "umap"
    batch_size: int = 8
    device: str = "auto"

    @classmethod
    def from_dict(cls, value: dict) -> "AnalysisConfig":
        if not isinstance(value, dict):
            raise ValueError("分析配置必须是对象")
        config = cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})
        config.model = str(config.model).strip()
        config.model_revision = str(config.model_revision).strip().lower()
        config.model_access = str(config.model_access).strip().lower()
        config.input_size = str(config.input_size)
        if not config.model:
            raise ValueError("模型名称或本地模型目录不能为空")
        if not _COMMIT_PATTERN.fullmatch(config.model_revision):
            raise ValueError("模型 revision 必须是完整的 40 位十六进制提交哈希")
        if config.model_access not in {"local_only", "download_if_missing"}:
            raise ValueError("模型访问策略必须是 local_only 或 download_if_missing")
        if config.background_mode not in {"original", "dimmed", "neutral_outside"}:
            raise ValueError("无效的背景模式")
        if config.input_size not in {"auto", "224", "336", "518"}:
            raise ValueError("输入尺寸必须是 auto/224/336/518")
        if isinstance(config.roi_expansion, bool):
            raise ValueError("ROI 外扩比例必须是数字")
        try:
            config.roi_expansion = float(config.roi_expansion)
        except (TypeError, ValueError) as exc:
            raise ValueError("ROI 外扩比例必须是数字") from exc
        if not math.isfinite(config.roi_expansion) or not 0 <= config.roi_expansion <= 2:
            raise ValueError("ROI 外扩比例必须在 0 到 2 之间")
        if not isinstance(config.mask_pooling, bool):
            raise ValueError("mask_pooling 必须是布尔值")
        if config.projection not in {"tsne", "umap"}:
            raise ValueError("降维方式必须是 tsne 或 umap")
        if config.device not in {"auto", "cuda", "cpu"}:
            raise ValueError("计算设备必须是 auto/cuda/cpu")
        if isinstance(config.batch_size, bool):
            raise ValueError("批大小必须是整数")
        try:
            batch_size = int(config.batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("批大小必须是整数") from exc
        config.batch_size = max(1, min(batch_size, 64))
        return config


def _installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for display_name, distribution in DEPENDENCY_DISTRIBUTIONS.items():
        try:
            versions[display_name] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[display_name] = "missing"
    return versions


def dependency_status() -> dict:
    missing = []
    for name in REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ModuleNotFoundError, ValueError):
            missing.append(name)
    return {
        "ready": not missing,
        "missing": missing,
        "versions": _installed_versions(),
        "message": "完整分析环境已就绪" if not missing else "缺少完整分析依赖：" + ", ".join(missing),
    }


def _preflight_projection(projection: str, item_count: int | None = None) -> None:
    if item_count is not None:
        projection = _effective_projection(projection, item_count)
    if projection != "umap":
        return
    try:
        importlib.import_module("umap")
    except Exception as exc:
        raise RuntimeError(
            "选择了 UMAP，但 umap-learn 或其本地依赖无法导入；"
            "请重新运行 setup.ps1，或明确改选 t-SNE"
        ) from exc


def _preflight_neighbor_search(item_count: int) -> None:
    if item_count <= APPROXIMATE_NEIGHBOR_THRESHOLD:
        return
    try:
        importlib.import_module("pynndescent")
    except Exception as exc:
        raise RuntimeError(
            "大型项目需要可扩展近邻依赖 pynndescent；请重新运行 setup.ps1"
        ) from exc


def _preflight_scoring_dependencies() -> None:
    modules = (
        "sklearn.linear_model",
        "sklearn.model_selection",
        "sklearn.neighbors",
        "sklearn.preprocessing",
    )
    try:
        for module in modules:
            importlib.import_module(module)
    except Exception as exc:
        raise RuntimeError(
            "scikit-learn 或其本地数值依赖无法导入；请重新运行 setup.ps1"
        ) from exc


def _local_model_directory(model: str) -> Path | None:
    try:
        candidate = Path(model).expanduser()
        return candidate.resolve() if candidate.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _is_cached_file(value) -> bool:
    return isinstance(value, str) and Path(value).is_file()


def model_status(config: AnalysisConfig, model_identity: dict | None = None) -> dict:
    """Return a cheap preflight result without downloading model files."""
    local_dir = _local_model_directory(config.model)
    if local_dir:
        has_config = (local_dir / "config.json").is_file()
        has_weights = any(local_dir.glob("*.safetensors"))
        cached = has_config and has_weights
        status_identity = model_identity or {"kind": "local", "path": str(local_dir)}
        return {
            "ready": cached,
            "cached": cached,
            "download_allowed": False,
            "identity": status_identity,
            "message": "本地模型已就绪" if cached else "本地模型目录缺少 config.json 或 safetensors 权重",
        }

    cached = False
    try:
        from huggingface_hub import try_to_load_from_cache

        config_file = try_to_load_from_cache(
            config.model, "config.json", revision=config.model_revision
        )
        weight_file = try_to_load_from_cache(
            config.model, "model.safetensors", revision=config.model_revision
        )
        cached = _is_cached_file(config_file) and _is_cached_file(weight_file)
    except (ImportError, OSError, ValueError):
        cached = False
    download_allowed = config.model_access == "download_if_missing"
    status_identity = model_identity or {
        "kind": "huggingface",
        "model": config.model,
        "revision": config.model_revision,
    }
    if cached:
        message = "固定 revision 的模型缓存已就绪"
    elif download_allowed:
        message = "模型尚未缓存；本次分析已明确允许下载"
    else:
        message = "模型尚未缓存，且当前配置禁止联网下载"
    return {
        "ready": cached or download_allowed,
        "cached": cached,
        "download_allowed": download_allowed,
        "identity": status_identity,
        "message": message,
    }


def _effective_size(config: AnalysisConfig, torch) -> int:
    if config.input_size != "auto":
        return int(config.input_size)
    if config.device != "cpu" and torch.cuda.is_available():
        try:
            memory = torch.cuda.get_device_properties(0).total_memory
            return 518 if memory >= 8 * 1024**3 else 336
        except Exception:
            return 336
    return 224


def _polygons(value, depth: int = 0) -> list[list[list[float]]]:
    if depth > 32 or not isinstance(value, (list, tuple)):
        return []
    if len(value) >= 3 and all(
        isinstance(point, (list, tuple)) and len(point) >= 2 and
        not isinstance(point[0], (list, tuple, dict)) and
        not isinstance(point[1], (list, tuple, dict))
        for point in value
    ):
        polygon: list[list[float]] = []
        for point in value:
            if any(isinstance(number, bool) for number in point[:2]):
                return []
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError, OverflowError):
                return []
            if not math.isfinite(x) or not math.isfinite(y):
                return []
            polygon.append([x, y])
        return [polygon]
    found: list[list[list[float]]] = []
    for child in value:
        found.extend(_polygons(child, depth + 1))
    return found


def _normalized_bbox(
    value, image_width: int, image_height: int
) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or image_width < 1
        or image_height < 1
    ):
        return None
    try:
        x1, y1, x2, y2 = (float(number) for number in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(number) for number in (x1, y1, x2, y2)):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    x1, x2 = max(0.0, x1), min(float(image_width), x2)
    y1, y2 = max(0.0, y1), min(float(image_height), y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _expanded_crop_box(
    bbox: tuple[float, float, float, float], image_width: int, image_height: int, expansion: float
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    extra_x = (x2 - x1) * expansion
    extra_y = (y2 - y1) * expansion
    left = max(0, min(image_width - 1, math.floor(x1 - extra_x)))
    top = max(0, min(image_height - 1, math.floor(y1 - extra_y)))
    right = max(left + 1, min(image_width, math.ceil(x2 + extra_x)))
    bottom = max(top + 1, min(image_height, math.ceil(y2 + extra_y)))
    return left, top, right, bottom


def _prepare_image(
    dataset: Dataset, item, size: int, config: AnalysisConfig, Image, ImageDraw, ImageEnhance
):
    preview = read_preview(dataset, item)
    if not preview:
        raise FileNotFoundError(f"找不到图像：{item.relative_path}")
    try:
        with Image.open(io.BytesIO(preview[0])) as source:
            if (source.width <= 0 or source.height <= 0 or
                    source.width * source.height > MAX_IMAGE_PIXELS):
                raise ValueError(
                    f"图像像素数超过 {MAX_IMAGE_PIXELS} 的安全上限：{item.relative_path}"
                )
            image = source.convert("RGB")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and "像素数超过" in str(exc):
            raise
        raise ValueError(f"图像无法解码：{item.relative_path}") from exc
    bbox = _normalized_bbox(item.metadata.get("bbox"), image.width, image.height)
    crop_box = (
        _expanded_crop_box(bbox, image.width, image.height, config.roi_expansion)
        if bbox
        else (0, 0, image.width, image.height)
    )
    crop = image.crop(crop_box)
    mask = Image.new("L", crop.size, 0 if bbox else 255)
    if bbox:
        polygons = _polygons(item.metadata.get("contours") or [])
        draw = ImageDraw.Draw(mask)
        if polygons:
            types = item.metadata.get("contour_types") or []
            max_x, max_y = max(0, crop.width - 1), max(0, crop.height - 1)
            for index, polygon in enumerate(polygons):
                local = [
                    (
                        min(max(point[0] - crop_box[0], 0.0), max_x),
                        min(max(point[1] - crop_box[1], 0.0), max_y),
                    )
                    for point in polygon
                ]
                kind = str(types[index] if index < len(types) else "Outer").lower()
                draw.polygon(local, fill=0 if kind == "inner" else 255)
        else:
            x1, y1, x2, y2 = bbox
            draw.rectangle(
                (x1 - crop_box[0], y1 - crop_box[1], x2 - crop_box[0], y2 - crop_box[1]),
                fill=255,
            )
    if config.background_mode == "neutral_outside" and bbox:
        neutral = Image.new("RGB", crop.size, (124, 116, 104))
        neutral.paste(crop, mask=mask)
        crop = neutral
    elif config.background_mode == "dimmed" and bbox:
        gray = crop.convert("L").convert("RGB")
        gray = ImageEnhance.Contrast(gray).enhance(0.45)
        gray = ImageEnhance.Brightness(gray).enhance(1.18)
        gray.paste(crop, mask=mask)
        crop = gray
    side = max(crop.size)
    padded = Image.new("RGB", (side, side), (124, 116, 104))
    offset = ((side - crop.width) // 2, (side - crop.height) // 2)
    padded.paste(crop, offset)
    padded_mask = Image.new("L", (side, side), 0)
    padded_mask.paste(mask, offset)
    return (
        padded.resize((size, size), Image.Resampling.LANCZOS),
        padded_mask.resize((size, size), Image.Resampling.BOX),
    )


def _tensor(image, np, torch):
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = (array - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)) / np.asarray(
        [0.229, 0.224, 0.225], dtype=np.float32
    )
    return torch.from_numpy(array).permute(2, 0, 1)


def _pool(tokens, masks, np, torch, use_mask: bool):
    patches = tokens[:, 1:, :]
    if not use_mask:
        return patches.mean(dim=1)
    grid = int(math.sqrt(patches.shape[1]))
    if grid * grid != patches.shape[1]:
        return tokens[:, 0, :]
    pooled = []
    for patch, mask in zip(patches, masks):
        weights = np.asarray(mask.resize((grid, grid)), dtype=np.float32).reshape(-1) / 255.0
        weights = torch.from_numpy(weights).to(device=patch.device, dtype=patch.dtype)
        if float(weights.sum()) <= 1e-8:
            weights[:] = 1
        pooled.append((patch * weights[:, None]).sum(0) / weights.sum())
    return torch.stack(pooled)


def score_feature_matrix(features, labels, np_module=None) -> dict:
    """Score an already extracted feature matrix; useful for deterministic validation."""
    if np_module is None:
        import numpy as np_module
    np = np_module
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.preprocessing import normalize

    features = np.asarray(features)
    raw_labels = np.asarray(labels)
    if features.ndim != 2 or len(features) < 2 or features.shape[1] < 1:
        raise ValueError("特征矩阵必须是至少 2 行的二维数组")
    if raw_labels.ndim != 1 or len(raw_labels) != len(features):
        raise ValueError("标签数组必须与特征矩阵行数一致")
    if not _feature_values_are_finite(features, np):
        raise ValueError("特征矩阵包含 NaN 或无穷值")
    try:
        labels = raw_labels.astype(np.int64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("标签必须是非负整数") from exc
    if not np.array_equal(raw_labels, labels) or (labels < 0).any():
        raise ValueError("标签必须是非负整数")

    x = _normalized_float32_features(features, np, normalize)
    unique, encoded = np.unique(labels, return_inverse=True)
    active_class_count = len(unique)
    _validate_scoring_scale(len(x), active_class_count)
    _preflight_neighbor_search(len(x))
    probabilities = np.zeros((len(x), active_class_count), dtype=np.float32)
    counts = np.bincount(encoded, minlength=active_class_count)
    if len(unique) > 1 and counts.min() >= 2:
        folds = min(5, int(counts.min()))
        classifier = LogisticRegression(
            C=1.0, class_weight="balanced", max_iter=1200, solver="lbfgs"
        )
        cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
        predicted_probabilities = cross_val_predict(
            classifier, x, encoded, cv=cv, method="predict_proba", n_jobs=1
        )
        probabilities[:] = predicted_probabilities
        del predicted_probabilities
    else:
        centroids = []
        for class_index in range(active_class_count):
            centroid = x[encoded == class_index].mean(axis=0)
            centroids.append(centroid / max(float(np.linalg.norm(centroid)), 1e-8))
        logits = x @ np.asarray(centroids).T * 8
        logits -= logits.max(axis=1, keepdims=True)
        np.exp(logits, out=logits)
        logits /= logits.sum(axis=1, keepdims=True)
        probabilities[:] = logits
        del logits

    neighbor_target = min(15, len(x) - 1)
    if neighbor_target > 0:
        candidate_count = min(len(x), neighbor_target + 1)
        indices, distances = _neighbor_candidates(x, candidate_count)
        local = _local_neighbor_distribution(
            indices, distances, encoded, active_class_count, neighbor_target, np
        )
    else:
        local = probabilities.copy()
    rows = np.arange(len(x))
    support = local[rows, encoded].copy()
    probabilities *= 0.7
    local *= 0.3
    probabilities += local
    combined = probabilities
    confidence = combined[rows, encoded]
    scores = np.clip((1 - confidence) * 100, 0, 100)
    return {
        "normalized_features": x,
        "scores": scores,
        "predicted": unique[combined.argmax(axis=1)],
        "confidence": confidence,
        "support": support,
    }


def _normalized_float32_features(features, np, normalize):
    """Normalize in-place when the caller already owns a float32 matrix."""

    element_count = int(features.size)
    float32_bytes = element_count * 4
    input_bytes = int(getattr(features, "nbytes", float32_bytes))
    needs_conversion = features.dtype != np.dtype(np.float32)
    # Row norms are the only material temporary used by sklearn's dense
    # normalize(copy=False) path.  Count both matrices when dtype conversion
    # is required so the guard reflects the actual peak, not just output size.
    norm_bytes = int(features.shape[0]) * 4
    finite_check_bytes = min(element_count, MAX_FINITE_CHECK_ELEMENTS)
    peak_bytes = (
        input_bytes + (float32_bytes if needs_conversion else 0) +
        max(norm_bytes, finite_check_bytes)
    )
    if peak_bytes > MAX_FEATURE_MATRIX_BYTES:
        raise ValueError(
            f"特征类型转换与归一化的峰值内存预计需要 "
            f"{peak_bytes / 1024**3:.1f} GiB；请使用 float32 特征或拆分项目"
        )
    converted = np.asarray(features, dtype=np.float32)
    if needs_conversion and not _feature_values_are_finite(converted, np):
        raise ValueError("特征矩阵转换为 float32 后包含 NaN 或无穷值")
    return normalize(converted, norm="l2", copy=False)


def _feature_values_are_finite(features, np) -> bool:
    """Bound the temporary boolean array used for finite-value validation."""

    columns = max(1, int(features.shape[1]))
    rows_per_chunk = max(1, MAX_FINITE_CHECK_ELEMENTS // columns)
    for start in range(0, len(features), rows_per_chunk):
        if not np.isfinite(features[start : start + rows_per_chunk]).all():
            return False
    return True


def _neighbor_candidates(features, candidate_count: int):
    if len(features) > APPROXIMATE_NEIGHBOR_THRESHOLD:
        from pynndescent import NNDescent

        graph = NNDescent(
            features,
            n_neighbors=candidate_count,
            metric="cosine",
            random_state=42,
            n_jobs=1,
            low_memory=True,
        )
        return graph.neighbor_graph
    from sklearn.neighbors import NearestNeighbors

    neighbors = NearestNeighbors(
        n_neighbors=candidate_count, metric="cosine"
    ).fit(features)
    distances, indices = neighbors.kneighbors(features)
    return indices, distances


def _local_neighbor_distribution(
    indices, distances, encoded_labels, class_count: int, neighbor_target: int, np
):
    local = np.zeros((len(encoded_labels), class_count), dtype=np.float32)
    for row in range(len(encoded_labels)):
        accepted = 0
        for distance, neighbor in zip(distances[row], indices[row]):
            neighbor = int(neighbor)
            if neighbor == row or not 0 <= neighbor < len(encoded_labels):
                continue
            distance = float(distance)
            if not math.isfinite(distance):
                continue
            local[row, encoded_labels[neighbor]] += 1.0 / (max(0.0, distance) + 0.05)
            accepted += 1
            if accepted >= neighbor_target:
                break
    local /= np.maximum(local.sum(axis=1, keepdims=True), 1e-8)
    return local


def _validate_scoring_scale(item_count: int, active_class_count: int) -> None:
    # CV prediction is float64 while the two persistent probability matrices
    # are float32.  Sixteen bytes/cell is a conservative peak estimate.
    estimated = int(item_count) * int(active_class_count) * 16
    if estimated > MAX_SCORING_WORKING_BYTES:
        gib = estimated / 1024**3
        raise ValueError(
            f"样本数与类别数的组合预计需要至少 {gib:.1f} GiB 评分内存；"
            "请拆分项目或减少本次分析类别"
        )


def _validate_analysis_scale(item_count: int, active_class_count: int, projection: str) -> None:
    _validate_scoring_scale(item_count, active_class_count)
    # The extracted float32 matrix is normalized in-place.  Include the row
    # norm scratch vector so a matrix exactly at the nominal cap cannot push
    # the actual resident peak over it.
    feature_elements = int(item_count) * 768
    estimated_features = feature_elements * 4 + max(
        int(item_count) * 4,
        min(feature_elements, MAX_FINITE_CHECK_ELEMENTS),
    )
    if estimated_features > MAX_FEATURE_MATRIX_BYTES:
        gib = estimated_features / 1024**3
        raise ValueError(
            f"特征矩阵预计需要 {gib:.1f} GiB；请拆分项目后再分析"
        )
    if projection == "tsne" and item_count > MAX_TSNE_ITEMS:
        raise ValueError(
            f"t-SNE 最多支持 {MAX_TSNE_ITEMS} 个复查项；"
            "请改用 UMAP 或拆分项目"
        )
    _preflight_neighbor_search(item_count)


def _effective_projection(projection: str, item_count: int) -> str:
    return "svd" if item_count < 4 else projection


def _project_features(features, projection: str, progress: Progress, np):
    if projection == "tsne" and len(features) > MAX_TSNE_ITEMS:
        raise ValueError(
            f"t-SNE 最多支持 {MAX_TSNE_ITEMS} 个复查项；请改用 UMAP 或拆分项目"
        )
    effective_projection = _effective_projection(projection, len(features))
    progress(0.9, f"正在计算 {effective_projection.upper()} 分布")
    if len(features) < 4:
        centered = features - features.mean(axis=0)
        left, singular_values, _right = np.linalg.svd(centered, full_matrices=False)
        coords = left[:, :2] * singular_values[:2]
        if coords.shape[1] == 1:
            coords = np.column_stack((coords[:, 0], np.zeros(len(coords))))
    elif projection == "umap":
        try:
            import umap
        except Exception as exc:
            raise RuntimeError(
                "选择了 UMAP，但当前环境缺少 umap-learn；请重新运行 setup.ps1，"
                "或明确改选 t-SNE"
            ) from exc
        coords = umap.UMAP(
            n_components=2,
            metric="cosine",
            n_neighbors=min(15, len(features) - 1),
            min_dist=0.12,
            random_state=42,
        ).fit_transform(features)
    elif projection == "tsne":
        from sklearn.manifold import TSNE

        perplexity = min(40.0, max(2.0, len(features) / 20), len(features) - 1.0)
        coords = TSNE(
            n_components=2,
            metric="cosine",
            init="random",
            perplexity=perplexity,
            learning_rate="auto",
            random_state=42,
        ).fit_transform(features)
    if not np.isfinite(coords).all():
        raise RuntimeError("降维结果包含无效数值")
    return coords.astype(np.float32)


def _score_and_project(features, labels, projection: str, progress: Progress, np):
    scored = score_feature_matrix(features, labels, np)
    coords = _project_features(scored["normalized_features"], projection, progress, np)
    return (
        coords,
        scored["scores"],
        scored["predicted"],
        scored["confidence"],
        scored["support"],
    )


def _file_sha256(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"模型文件在读取期间发生变化：{path}")
    return digest.hexdigest()


def _model_identity(config: AnalysisConfig) -> dict:
    local_dir = _local_model_directory(config.model)
    if not local_dir:
        identity = {
            "kind": "huggingface",
            "model": config.model,
            "revision": config.model_revision,
            "files": [],
        }
        try:
            from huggingface_hub import try_to_load_from_cache

            for name in ("config.json", "model.safetensors"):
                cached = try_to_load_from_cache(
                    config.model, name, revision=config.model_revision
                )
                if _is_cached_file(cached):
                    path = Path(cached)
                    identity["files"].append(
                        {"name": name, "size": path.stat().st_size,
                         "sha256": _file_sha256(path)}
                    )
        except (ImportError, OSError, ValueError):
            pass
        return identity
    files = []
    identity_paths = {
        local_dir / "config.json",
        *local_dir.glob("*.safetensors"),
        *local_dir.glob("*.safetensors.index.json"),
    }
    for path in sorted(identity_paths):
        if path.is_file():
            files.append(
                {"name": path.name, "size": path.stat().st_size, "sha256": _file_sha256(path)}
            )
    return {"kind": "local", "path": str(local_dir), "files": files}


def _download_pinned_model_files(config: AnalysisConfig) -> None:
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=config.model,
            revision=config.model_revision,
            allow_patterns=["config.json", "model.safetensors"],
        )
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(
            "无法下载固定 revision 的 DINOv2 模型；请检查网络、代理和磁盘空间"
        ) from exc


def _preview_identity(item) -> str:
    raw = str(item.metadata.get("archive_entry") or item.relative_path).replace("\\", "/")
    normalized = posixpath.normpath(raw)
    return normalized.casefold() if os.name == "nt" else normalized


def _uses_external_previews(dataset: Dataset) -> bool:
    source_type = str(getattr(dataset, "source_type", ""))
    return source_type.startswith("srproj:") or source_type.startswith("saige-json:")


def _preview_payload_digest(payload: bytes | memoryview) -> str:
    digest = hashlib.sha256()
    view = memoryview(payload)
    for offset in range(0, len(view), 1024 * 1024):
        digest.update(view[offset : offset + 1024 * 1024])
    return digest.hexdigest()


def preview_content_digest(dataset: Dataset, item) -> str:
    """Return the current bounded preview content hash for stale-result checks."""

    preview = read_preview(dataset, item)
    if preview is None:
        raise RuntimeError(f"图像缺失、过大或不可读：{item.relative_path}")
    return _preview_payload_digest(preview[0])


def _analysis_input_snapshot(dataset: Dataset) -> tuple[str, dict[str, str]]:
    """Hash each distinct external preview and the ordered analysis input set."""

    if not _uses_external_previews(dataset):
        return str(dataset.source_hash or ""), {}

    digest = hashlib.sha256()
    digest.update(f"source:{dataset.source_hash or ''}\n".encode("utf-8"))
    seen: set[str] = set()
    preview_digests: dict[str, str] = {}
    missing: list[str] = []
    for item in dataset.items:
        identity = _preview_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        identity_bytes = identity.encode("utf-8")
        digest.update(len(identity_bytes).to_bytes(8, "big"))
        digest.update(identity_bytes)
        preview = read_preview(dataset, item)
        if preview is None:
            digest.update(b"\x00missing")
            if len(missing) < 5:
                missing.append(str(item.relative_path))
            continue
        digest.update(b"\x01present")
        payload = memoryview(preview[0])
        preview_digests[identity] = _preview_payload_digest(payload)
        digest.update(len(payload).to_bytes(8, "big"))
        for offset in range(0, len(payload), 1024 * 1024):
            digest.update(payload[offset : offset + 1024 * 1024])
    if missing:
        detail = ", ".join(missing)
        raise RuntimeError(
            f"分析所需图像缺失、过大或不可读：{detail}；请修复路径后重新载入数据源"
        )
    return digest.hexdigest(), preview_digests


def analysis_input_fingerprint(dataset: Dataset) -> str:
    """Hash every distinct external image referenced by project-style datasets."""

    return _analysis_input_snapshot(dataset)[0]


def analysis_fingerprint(
    dataset: Dataset,
    config: AnalysisConfig,
    size: int,
    dependency_versions: dict[str, str] | None = None,
    model_identity: dict | None = None,
    input_fingerprint: str | None = None,
    execution_identity: dict[str, str] | None = None,
) -> str:
    fingerprint_config = asdict(config)
    fingerprint_config.pop("batch_size", None)
    fingerprint_config.pop("device", None)
    fingerprint_config.pop("model_access", None)
    payload = {
        "cache_schema": CACHE_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "algorithm_version": ANALYSIS_ALGORITHM_VERSION,
        "source": dataset.source_hash,
        "analysis_inputs": (
            input_fingerprint
            if input_fingerprint is not None
            else analysis_input_fingerprint(dataset)
        ),
        "classes": dataset.classes,
        "items": [
            {"id": item.id, "path": item.relative_path, "label": item.original_label}
            for item in dataset.items
        ],
        "config": fingerprint_config,
        "model_identity": model_identity or _model_identity(config),
        "execution": execution_identity or {"device": config.device},
        "effective_size": size,
        "dependencies": dependency_versions or _installed_versions(),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_npy_header(stream, np) -> tuple[tuple[int, ...], object, int]:
    prefix = stream.read(8)
    if len(prefix) != 8 or prefix[:6] != b"\x93NUMPY":
        raise ValueError("缓存数组缺少 NPY 文件头")
    major = prefix[6]
    if major == 1:
        length_bytes = stream.read(2)
        if len(length_bytes) != 2:
            raise ValueError("缓存数组头不完整")
        header_length = struct.unpack("<H", length_bytes)[0]
        encoding = "latin1"
    elif major in {2, 3}:
        length_bytes = stream.read(4)
        if len(length_bytes) != 4:
            raise ValueError("缓存数组头不完整")
        header_length = struct.unpack("<I", length_bytes)[0]
        encoding = "utf-8" if major == 3 else "latin1"
    else:
        raise ValueError("缓存数组使用不支持的 NPY 版本")
    if header_length > 64 * 1024:
        raise ValueError("缓存数组头过大")
    header_bytes = stream.read(header_length)
    if len(header_bytes) != header_length:
        raise ValueError("缓存数组头被截断")
    try:
        header = ast.literal_eval(header_bytes.decode(encoding).strip())
    except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("缓存数组头格式无效") from exc
    if not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}:
        raise ValueError("缓存数组头字段无效")
    shape = header["shape"]
    if (not isinstance(shape, tuple) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in shape)):
        raise ValueError("缓存数组 shape 无效")
    if not isinstance(header["fortran_order"], bool):
        raise ValueError("缓存数组布局标记无效")
    dtype = np.dtype(header["descr"])
    if dtype.hasobject or dtype.kind not in "fiu" or dtype.itemsize < 1 or dtype.itemsize > 8:
        raise ValueError("缓存数组 dtype 不安全")
    return shape, dtype, stream.tell()


def _validate_cache_archive(raw, item_count: int, np) -> bool:
    expected_shapes = {
        "coords.npy": (item_count, 2),
        "scores.npy": (item_count,),
        "predicted.npy": (item_count,),
        "confidence.npy": (item_count,),
        "support.npy": (item_count,),
    }
    raw.seek(0)
    with zipfile.ZipFile(raw) as archive:
        infos = archive.infolist()
        if (len(infos) != len(expected_shapes) or any(info.is_dir() for info in infos) or
                len({info.filename for info in infos}) != len(infos) or
                {info.filename for info in infos} != set(expected_shapes)):
            return False
        for info in infos:
            maximum_data = math.prod(expected_shapes[info.filename]) * 8
            if info.file_size > maximum_data + 64 * 1024 + 16:
                return False
            with archive.open(info) as stream:
                shape, dtype, header_size = _read_npy_header(stream, np)
            if shape != expected_shapes[info.filename]:
                return False
            data_size = math.prod(shape) * dtype.itemsize
            if info.file_size != header_size + data_size:
                return False
    return True


def _load_cache(cache_path: Path, item_count: int, class_count: int, np):
    try:
        with cache_path.open("rb") as raw:
            before = os.fstat(raw.fileno())
            if not _validate_cache_archive(raw, item_count, np):
                return None
            raw.seek(0)
            with np.load(raw, allow_pickle=False) as saved:
                names = ("coords", "scores", "predicted", "confidence", "support")
                arrays = {name: saved[name].copy() for name in names}
            after = os.fstat(raw.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return None
        if arrays["coords"].shape != (item_count, 2):
            return None
        vectors = ("scores", "predicted", "confidence", "support")
        if any(arrays[name].shape != (item_count,) for name in vectors):
            return None
        if not all(np.isfinite(value).all() for value in arrays.values()):
            return None
        predicted = arrays["predicted"]
        if not np.array_equal(predicted, predicted.astype(np.int64)):
            return None
        if (predicted < 0).any() or (predicted >= class_count).any():
            return None
        if (arrays["scores"] < 0).any() or (arrays["scores"] > 100).any():
            return None
        if any(
            (arrays[name] < 0).any() or (arrays[name] > 1).any()
            for name in ("confidence", "support")
        ):
            return None
        arrays["predicted"] = predicted.astype(np.int64)
        return arrays
    except (OSError, EOFError, KeyError, ValueError, TypeError, zipfile.BadZipFile):
        return None


def _save_cache(cache_path: Path, arrays: dict, np) -> None:
    temporary = cache_path.with_name(f".{cache_path.stem}.{uuid.uuid4().hex}.tmp.npz")
    try:
        np.savez_compressed(temporary, **arrays)
        temporary.replace(cache_path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_model(config: AnalysisConfig, dtype, AutoModel, preflight: dict | None = None):
    local_dir = _local_model_directory(config.model)
    status = preflight or model_status(config)
    if not status["ready"]:
        raise RuntimeError(
            "固定 revision 的 DINOv2 模型尚未安装。请运行 setup.ps1 预取模型，"
            "或在本次分析配置中明确设置 model_access=download_if_missing。"
        )
    model_reference = str(local_dir) if local_dir else config.model
    kwargs = {
        "dtype": dtype,
        "local_files_only": bool(local_dir) or config.model_access == "local_only",
        "use_safetensors": True,
        "trust_remote_code": False,
    }
    if not local_dir:
        kwargs["revision"] = config.model_revision
    try:
        return AutoModel.from_pretrained(model_reference, **kwargs)
    except OSError as exc:
        if config.model_access == "download_if_missing" and not local_dir:
            raise RuntimeError(
                "无法下载或加载固定 revision 的 DINOv2 模型；请检查网络、代理和磁盘空间，"
                "也可以运行 setup.ps1 后改用 local_only。"
            ) from exc
        raise RuntimeError(
            "本地 DINOv2 缓存不完整或不可读；请重新运行 setup.ps1 下载固定 revision。"
        ) from exc


def _forward_model(model, pixels):
    """Call a model with positional interpolation when its forward API supports it."""
    try:
        parameter = inspect.signature(model.forward).parameters.get(
            "interpolate_pos_encoding"
        )
    except (AttributeError, TypeError, ValueError):
        parameter = None
    supports_interpolation = (
        parameter is not None
        and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    )
    kwargs = {"pixel_values": pixels}
    if supports_interpolation:
        kwargs["interpolate_pos_encoding"] = True
    return model(**kwargs)


def _execution_identity(torch, device_name: str) -> dict[str, str]:
    use_cuda = device_name == "cuda"
    identity = {
        "device": device_name,
        "dtype": "float16" if use_cuda else "float32",
    }
    if use_cuda:
        identity["cuda_runtime"] = str(getattr(torch.version, "cuda", "unknown"))
        try:
            identity["cuda_device"] = str(torch.cuda.get_device_name(0))
            identity["cuda_capability"] = ".".join(
                map(str, torch.cuda.get_device_capability(0))
            )
        except Exception:
            identity["cuda_device"] = "unknown"
    return identity


def _is_cuda_runtime_failure(error: Exception, torch) -> bool:
    """Return true only for failures that plausibly come from CUDA execution."""
    out_of_memory = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
    if out_of_memory is not None and isinstance(error, out_of_memory):
        return True
    message = f"{type(error).__name__}: {error}".lower()
    markers = (
        "cuda", "cudnn", "cublas", "cusolver", "cusparse", "gpu",
        "device-side", "device side", "no kernel image", "nvidia driver",
    )
    return any(marker in message for marker in markers)


def _build_item_updates(
    items, classes, coords, scores, predicted, confidence, support,
    preview_digests: dict[str, str] | None = None,
) -> list[dict]:
    updates = []
    for index, item in enumerate(items):
        update = {
                "id": item.id,
                "x": float(coords[index][0]),
                "y": float(coords[index][1]),
                "suspicion_score": round(float(scores[index]), 2),
                "suggested_label": classes[int(predicted[index])],
                "label_confidence": round(float(confidence[index]), 5),
                "neighbor_support": round(float(support[index]), 5),
                "analysis_state": "analyzed",
            }
        if preview_digests is not None:
            preview_digest = preview_digests.get(_preview_identity(item))
            if preview_digest is not None:
                update["_analysis_preview_sha256"] = preview_digest
        updates.append(update)
    return updates


def _assert_source_unchanged(dataset: Dataset) -> None:
    if dataset.source is None:
        return
    if not dataset.source_hash:
        raise RuntimeError("源数据缺少加载时指纹；请重新载入数据集后再分析。")
    try:
        current = source_fingerprint(dataset.source)
    except OSError as exc:
        raise RuntimeError("无法重新读取源数据；请确认文件仍存在并重新载入数据集。") from exc
    if not hmac.compare_digest(str(dataset.source_hash), current):
        raise RuntimeError("分析期间源数据已被外部修改；已停止使用结果，请重新载入数据集。")


def _assert_analysis_inputs_unchanged(dataset: Dataset, expected: str) -> None:
    _assert_source_unchanged(dataset)
    current = analysis_input_fingerprint(dataset)
    if not hmac.compare_digest(expected, current):
        raise RuntimeError("分析引用的图像内容已被外部修改；已停止使用结果，请重新载入数据集。")


def analyze(dataset: Dataset, config: AnalysisConfig, cache_dir: Path, progress: Progress) -> dict:
    status = dependency_status()
    if not status["ready"]:
        raise RuntimeError(status["message"] + "。请运行 setup.ps1 安装完整分析环境。")
    _preflight_scoring_dependencies()
    items = tuple(dataset.items)
    classes = tuple(dataset.classes)
    if len(items) < 2:
        raise ValueError("至少需要 2 个已标注样本")
    if not classes:
        raise ValueError("数据集中没有可用类别")
    _preflight_projection(config.projection, len(items))
    import numpy as np
    import torch
    from PIL import Image, ImageDraw, ImageEnhance
    from transformers import AutoModel

    class_indices = {label: index for index, label in enumerate(classes)}
    if len(class_indices) != len(classes):
        raise ValueError("数据集包含重复类别名，无法安全分析")
    try:
        labels = np.asarray([class_indices[item.original_label] for item in items], dtype=np.int32)
    except KeyError as exc:
        raise ValueError("样本原始标签不在数据集类别列表中") from exc
    _validate_analysis_scale(len(items), len(set(labels.tolist())), config.projection)

    _assert_source_unchanged(dataset)
    input_fingerprint, preview_digests = _analysis_input_snapshot(dataset)
    size = _effective_size(config, torch)
    cuda_available = bool(torch.cuda.is_available())
    if config.device == "cuda" and not cuda_available:
        raise RuntimeError("已选择 CUDA，但当前环境未检测到可用 GPU")
    if config.device == "auto":
        device_candidates = ["cuda", "cpu"] if cuda_available else ["cpu"]
        fallback_reason = None if cuda_available else "当前环境未检测到可用 CUDA GPU"
    else:
        device_candidates = [config.device]
        fallback_reason = None
    model_preflight = model_status(config)
    if not model_preflight["cached"] and model_preflight["download_allowed"]:
        progress(0.01, "正在下载固定 revision 的 DINOv2 模型文件")
        _download_pinned_model_files(config)
        model_preflight = model_status(config)
    if not model_preflight["ready"] or not model_preflight["cached"]:
        raise RuntimeError(
            "固定 revision 的 DINOv2 模型尚未安装。请运行 setup.ps1 预取模型，"
            "或在本次分析配置中明确设置 model_access=download_if_missing。"
        )
    model_identity = _model_identity(config)
    if len(model_identity.get("files") or []) < 2:
        raise RuntimeError("固定模型配置或权重缺失，无法建立可验证的模型身份")
    model_preflight = model_status(config, model_identity)
    cache_dir.mkdir(parents=True, exist_ok=True)
    arrays = None
    feature_matrix = None
    cache_hit = False
    cache_path = None
    execution_identity = None

    for device_name in device_candidates:
        use_cuda = device_name == "cuda"
        execution_identity = _execution_identity(torch, device_name)
        fingerprint = analysis_fingerprint(
            dataset,
            config,
            size,
            status["versions"],
            model_identity=model_identity,
            input_fingerprint=input_fingerprint,
            execution_identity=execution_identity,
        )
        candidate_cache = cache_dir / f"analysis-v{CACHE_SCHEMA_VERSION}-{fingerprint}.npz"
        arrays = (
            _load_cache(candidate_cache, len(items), len(classes), np)
            if candidate_cache.exists() else None
        )
        if arrays is not None:
            cache_hit = True
            cache_path = candidate_cache
            _assert_analysis_inputs_unchanged(dataset, input_fingerprint)
            progress(0.98, "已加载版本匹配的分析缓存")
            break

        device = torch.device(execution_identity["device"])
        dtype = torch.float16 if use_cuda else torch.float32
        model = None
        pixels = None
        try:
            progress(
                0.02,
                f"正在加载 DINOv2（{device.type}，{size}px）：{model_preflight['message']}",
            )
            model = _load_model(config, dtype, AutoModel, model_preflight).eval().to(
                device=device, dtype=dtype
            )
            if _model_identity(config) != model_identity:
                raise RuntimeError("模型文件在加载期间发生变化；已停止分析，请重试")
            feature_matrix = None
            total = len(items)
            for start in range(0, total, config.batch_size):
                images, masks = [], []
                for item in items[start : start + config.batch_size]:
                    image, mask = _prepare_image(
                        dataset, item, size, config, Image, ImageDraw, ImageEnhance
                    )
                    images.append(_tensor(image, np, torch))
                    masks.append(mask)
                pixels = torch.stack(images).to(device=device, dtype=dtype)
                with torch.inference_mode(), torch.autocast(
                    device_type=device.type, dtype=dtype, enabled=use_cuda
                ):
                    # Request interpolation on model implementations that expose
                    # the option; older transformers releases interpolate without
                    # accepting this forward keyword.
                    tokens = _forward_model(model, pixels).last_hidden_state
                    pooled = _pool(tokens, masks, np, torch, config.mask_pooling)
                batch_features = pooled.float().cpu().numpy()
                if feature_matrix is None:
                    feature_bytes = total * int(batch_features.shape[1]) * 4
                    if feature_bytes > MAX_FEATURE_MATRIX_BYTES:
                        raise ValueError(
                            f"实际特征矩阵需要 {feature_bytes / 1024**3:.1f} GiB；"
                            "请拆分项目后再分析"
                        )
                    feature_matrix = np.empty(
                        (total, int(batch_features.shape[1])), dtype=np.float32
                    )
                feature_matrix[start : start + len(batch_features)] = batch_features
                progress(
                    0.05 + 0.72 * min(start + len(images), total) / total,
                    f"正在提取特征 {min(start + len(images), total)} / {total}",
                )
        except Exception as error:
            model = None
            pixels = None
            if (
                config.device == "auto"
                and device_name == "cuda"
                and _is_cuda_runtime_failure(error, torch)
            ):
                fallback_reason = (
                    str(error).strip().replace("\r", " ").replace("\n", " ")[:400]
                    or type(error).__name__
                )
                progress(0.02, f"GPU 分析不可用，正在自动切换到 CPU：{fallback_reason}")
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                continue
            if fallback_reason and device_name == "cpu":
                raise RuntimeError(
                    f"GPU 不可用后已自动改用 CPU，但 CPU 分析也失败：{error}"
                ) from error
            raise
        cache_path = candidate_cache
        break
    else:
        raise RuntimeError("没有可用的分析计算设备")

    if arrays is None:
        if feature_matrix is None:
            raise RuntimeError("特征提取未产生可用结果")
        _assert_analysis_inputs_unchanged(dataset, input_fingerprint)
        coords, scores, predicted, confidence, support = _score_and_project(
            feature_matrix, labels, config.projection, progress, np
        )
        arrays = {
            "coords": coords,
            "scores": scores,
            "predicted": predicted,
            "confidence": confidence,
            "support": support,
        }
        _save_cache(cache_path, arrays, np)
        try:
            _assert_analysis_inputs_unchanged(dataset, input_fingerprint)
        except RuntimeError:
            cache_path.unlink(missing_ok=True)
            raise

    progress(1.0, "分析完成")
    return {
        "effective_input_size": size,
        "effective_projection": _effective_projection(config.projection, len(items)),
        "cache": str(cache_path),
        "cache_hit": cache_hit,
        "cache_schema": CACHE_SCHEMA_VERSION,
        "algorithm_version": ANALYSIS_ALGORITHM_VERSION,
        "model_identity": model_identity,
        "execution": execution_identity,
        "device_fallback": (
            {"from": "cuda", "to": "cpu", "reason": fallback_reason}
            if fallback_reason and execution_identity["device"] == "cpu" else None
        ),
        "config": asdict(config),
        "item_updates": _build_item_updates(
            items,
            classes,
            arrays["coords"],
            arrays["scores"],
            arrays["predicted"],
            arrays["confidence"],
            arrays["support"],
            preview_digests,
        ),
    }
