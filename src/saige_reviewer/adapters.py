from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import xml.etree.ElementTree as ET
import zipfile
import io
from pathlib import Path

from .domain import Dataset, ReviewItem

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
MAX_PROJECT_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TOTAL_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_DATASET_ITEMS = 100_000
MAX_CLASSES = 10_000
MAX_ARCHIVE_ENTRIES = 200_000
MAX_JSON_CANDIDATES = 1_024
MAX_FOLDER_ENTRIES = 200_000
IMAGE_LEVEL_PROJECT_TYPES = frozenset({"iad", "classification", "classify", "cls"})


def _invalid_json_constant(value: str):
    raise ValueError(f"JSON 包含非标准数值：{value}")


def _unique_json_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"JSON 对象包含重复键：{key}")
        value[key] = child
    return value


def strict_json_loads(payload: str | bytes) -> object:
    try:
        value = json.loads(
            payload,
            parse_constant=_invalid_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except RecursionError as exc:
        raise ValueError("JSON 嵌套层级过深") from exc
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("JSON 包含超出有限范围的数字")
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return value


def _finite_float(value, *, description: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{description}不是有效数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{description}必须是有限数字")
    return result


def _finite_add(left: float, right: float, *, description: str) -> float:
    result = left + right
    if not math.isfinite(result):
        raise ValueError(f"{description}超出有限范围")
    return result


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _assert_same_file_stat(before: os.stat_result, after: os.stat_result, path: Path) -> None:
    if _stat_identity(before) != _stat_identity(after):
        raise OSError(f"读取期间文件发生变化：{path}")


def _hash_stream(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    path = path.resolve()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest = _hash_stream(stream)
        after = os.fstat(stream.fileno())
    _assert_same_file_stat(before, after, path)
    _assert_same_file_stat(before, path.stat(), path)
    return digest


def _read_project_snapshot(path: Path) -> tuple[bytes, str]:
    """Read a bounded ordinary project file and bind its digest to those bytes."""

    path = path.resolve()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_PROJECT_BYTES:
            raise ValueError(
                f"项目文件超过 {MAX_PROJECT_BYTES // (1024 * 1024)} MiB 读取上限：{path}"
            )
        payload = stream.read(MAX_PROJECT_BYTES + 1)
        after = os.fstat(stream.fileno())
    _assert_same_file_stat(before, after, path)
    _assert_same_file_stat(before, path.stat(), path)
    if len(payload) > MAX_PROJECT_BYTES:
        raise ValueError(
            f"项目文件超过 {MAX_PROJECT_BYTES // (1024 * 1024)} MiB 读取上限：{path}"
        )
    return payload, hashlib.sha256(payload).hexdigest()


def _assert_source_digest(path: Path, expected: str) -> None:
    if not hmac.compare_digest(sha256(path), expected):
        raise OSError(f"解析期间数据源发生变化：{path}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _validate_folder_layout(root: Path) -> None:
    """Reject folder layouts whose label/export meaning would be ambiguous."""

    pending = [root]
    entry_count = 0
    while pending:
        current = pending.pop()
        for entry in current.iterdir():
            entry_count += 1
            if entry_count > MAX_FOLDER_ENTRIES:
                raise ValueError(
                    f"文件夹数据源超过 {MAX_FOLDER_ENTRIES} 个目录项的安全上限"
                )
            if _is_link(entry):
                raise ValueError(f"文件夹数据集不支持符号链接或目录联接：{entry}")
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise ValueError(f"文件夹数据集包含不支持的文件类型：{entry}")
            elif current == root and entry.suffix.lower() in IMAGE_EXTENSIONS:
                raise ValueError(f"数据集根目录中的图像没有类别目录，无法确定标签：{entry}")


def _image_paths(root: Path) -> list[Path]:
    resolved_root = root.resolve()
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if len(paths) >= MAX_DATASET_ITEMS:
            raise ValueError(f"数据集超过 {MAX_DATASET_ITEMS} 个复查项的安全上限")
        paths.append(path)
    paths.sort()
    for path in paths:
        if not _is_within(path.resolve(), resolved_root):
            raise ValueError(f"图像路径越出数据集目录：{path}")
    return paths


def folder_fingerprint(root: Path) -> str:
    root = root.resolve()
    _validate_folder_layout(root)
    digest = hashlib.sha256()
    class_paths = sorted(path for path in root.iterdir() if path.is_dir())
    if len(class_paths) > MAX_CLASSES:
        raise ValueError(f"数据集超过 {MAX_CLASSES} 个类别的安全上限")
    for class_path in class_paths:
        if not _is_within(class_path.resolve(), root):
            raise ValueError(f"类别目录越出数据集目录：{class_path}")
        digest.update(b"class\0")
        digest.update(class_path.name.encode("utf-8"))
        digest.update(b"\0")
    for path in _image_paths(root):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        before = path.stat()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError(f"读取期间文件发生变化：{path}")
        digest.update(b"\0")
    return digest.hexdigest()


def source_fingerprint(path: Path) -> str:
    return folder_fingerprint(path) if path.is_dir() else sha256(path)


def _layout(index: int, class_index: int) -> tuple[float, float]:
    angle = index * 2.399963 + class_index * 0.55
    radius = 20 + 6 * math.sqrt(index + 1)
    return class_index * 75 + math.cos(angle) * radius, math.sin(angle) * radius


def load_folder(root: Path) -> Dataset:
    root = root.resolve()
    source_hash = folder_fingerprint(root)
    class_paths = sorted(path for path in root.iterdir() if path.is_dir())
    if len(class_paths) > MAX_CLASSES:
        raise ValueError(f"数据集超过 {MAX_CLASSES} 个类别的安全上限")
    for path in class_paths:
        if not _is_within(path.resolve(), root):
            raise ValueError(f"类别目录越出数据集目录：{path}")
    classes = [path.name for path in class_paths]
    class_indices = {name: index for index, name in enumerate(classes)}
    items: list[ReviewItem] = []
    for path in _image_paths(root):
        relative_path = path.relative_to(root)
        label = relative_path.parts[0]
        if label not in class_indices:
            raise OSError(f"枚举期间文件夹类别发生变化：{label}")
        class_index = class_indices[label]
        relative = str(relative_path)
        x, y = _layout(len(items), class_index)
        items.append(ReviewItem(str(len(items)), relative, label, None, None, x, y,
                                f"/api/preview/{len(items)}"))
    final_hash = folder_fingerprint(root)
    if not hmac.compare_digest(source_hash, final_hash):
        raise OSError(f"枚举期间文件夹数据源发生变化：{root}")
    return Dataset(root.name, root.resolve(), source_hash, classes, items, "folder")


def _class_name(raw: str | None, index: int) -> str:
    value = raw or f"class_{index}"
    match = re.fullmatch(r"\(\d+\)(.+)", value)
    return match.group(1) if match else value


def _xml_scalar_text(node: ET.Element | None, *, description: str) -> str:
    if node is None:
        return ""
    fragments: list[str] = []

    def collect(value: str | None) -> None:
        if value is not None and value.strip():
            fragments.append(value.strip())

    collect(node.text)
    for child in node:
        if child.tag not in {ET.Comment, ET.ProcessingInstruction}:
            raise ValueError(f"{description}包含不允许的嵌套 XML 元素")
        collect(child.tail)
    if len(fragments) > 1:
        raise ValueError(f"{description}包含多个非空文本片段")
    return fragments[0] if fragments else ""


def _class_index(node: ET.Element | None, *, description: str, class_count: int) -> int:
    value = _xml_scalar_text(node, description=description)
    if not re.fullmatch(r"\d+", value):
        raise ValueError(f"{description}缺少有效的非负整数类别索引")
    index = int(value)
    if not 0 <= index < class_count:
        raise ValueError(
            f"{description}引用越界类别索引 {index}（项目共有 {class_count} 个类别）"
        )
    return index


def load_srproj(path: Path) -> Dataset:
    path = path.resolve()
    payload, source_hash = _read_project_snapshot(path)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    root = ET.fromstring(payload, parser=parser)
    class_nodes = root.findall("./ClassGroup/Class")
    if len(class_nodes) > MAX_CLASSES:
        raise ValueError(f"srproj 超过 {MAX_CLASSES} 个类别的安全上限")
    classes = [_class_name(node.findtext("Name"), i) for i, node in enumerate(class_nodes)]
    seen_classes: set[str] = set()
    duplicates: set[str] = set()
    for name in classes:
        (duplicates if name in seen_classes else seen_classes).add(name)
    if duplicates:
        raise ValueError(
            "srproj 含有重复的类别显示名，无法安全映射类别索引：" +
            ", ".join(sorted(duplicates))
        )
    items: list[ReviewItem] = []
    images = root.findall("./ImageGroup/Image")
    if len(images) > MAX_DATASET_ITEMS:
        raise ValueError(f"srproj 超过 {MAX_DATASET_ITEMS} 个图像记录的安全上限")
    for image_no, image in enumerate(images):
        image_path = image.findtext("Path") or ""
        labels = image.findall("./LabelGroup/Label")
        # Classification srproj stores the label directly on the Image node.
        if not labels and image.find("ClassIndexOfLabel") is not None:
            class_index = _class_index(
                image.find("ClassIndexOfLabel"),
                description=f"图像 {image_path} 的 ClassIndexOfLabel",
                class_count=len(classes),
            )
            label = classes[class_index]
            item_id = str(len(items))
            x, y = _layout(len(items), class_index)
            if len(items) >= MAX_DATASET_ITEMS:
                raise ValueError(f"srproj 超过 {MAX_DATASET_ITEMS} 个复查项的安全上限")
            items.append(ReviewItem(item_id, image_path, label, None, None, x, y,
                                    f"/api/preview/{item_id}",
                                    metadata={"image_no": image_no, "label_no": None}))
        for label_no, node in enumerate(labels):
            class_index = _class_index(
                node.find("ClassIndex"),
                description=f"图像 {image_path} 的第 {label_no + 1} 个标注",
                class_count=len(classes),
            )
            label = classes[class_index]
            item_id = str(len(items))
            x, y = _layout(len(items), class_index)
            contours = []
            contour_types = []
            for contour in node.findall("./ContourGroup/Contour"):
                points = [[_finite_float(point.attrib["X"], description="轮廓 X 坐标"),
                           _finite_float(point.attrib["Y"], description="轮廓 Y 坐标")]
                          for point in contour.findall("Point")
                          if "X" in point.attrib and "Y" in point.attrib]
                if len(points) >= 3:
                    contours.append(points)
                    contour_types.append(contour.attrib.get("Type", "Outer"))
            xs = [point[0] for contour in contours for point in contour]
            ys = [point[1] for contour in contours for point in contour]
            metadata = {
                "image_no": image_no,
                "label_no": label_no,
                "contours": contours,
                "contour_types": contour_types,
            }
            if xs and ys:
                metadata["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
            coordinate = node.find("Coordinate")
            if coordinate is not None and all(key in coordinate.attrib for key in ("X", "Y", "Width", "Height")):
                x0 = _finite_float(coordinate.attrib["X"], description="标注 X 坐标")
                y0 = _finite_float(coordinate.attrib["Y"], description="标注 Y 坐标")
                width = _finite_float(coordinate.attrib["Width"], description="标注宽度")
                height = _finite_float(coordinate.attrib["Height"], description="标注高度")
                metadata["bbox"] = [
                    x0,
                    y0,
                    _finite_add(x0, width, description="标注右边界"),
                    _finite_add(y0, height, description="标注下边界"),
                ]
            if len(items) >= MAX_DATASET_ITEMS:
                raise ValueError(f"srproj 超过 {MAX_DATASET_ITEMS} 个复查项的安全上限")
            items.append(ReviewItem(item_id, image_path, label, None, None, x, y,
                                    f"/api/preview/{item_id}", metadata=metadata))
    project_type = (root.findtext("Type") or "unknown").lower()
    _assert_source_digest(path, source_hash)
    return Dataset(path.stem, path, source_hash, classes, items, f"srproj:{project_type}")


def _json_classes(project: dict) -> list[str]:
    identities: dict[str, dict[str, object]] = {}
    names_by_id: dict[tuple[type, object], str] = {}

    def collect_identity(node) -> None:
        if not isinstance(node, dict) or not node.get("className"):
            return
        name = str(node["className"])
        if name not in identities and len(identities) >= MAX_CLASSES:
            raise ValueError(f"Saige 项目超过 {MAX_CLASSES} 个类别的安全上限")
        known = identities.setdefault(name, {})
        for key in ("classId", "classColor"):
            value = node.get(key)
            if value is None:
                continue
            if key == "classId":
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise ValueError(f"Saige 类别 {name} 的 classId 必须是字符串或整数")
                identity_key = (type(value), value)
                previous_name = names_by_id.get(identity_key)
                if previous_name is not None and previous_name != name:
                    raise ValueError(
                        f"Saige 项目的 classId {value} 同时属于 {previous_name} 和 {name}"
                    )
                names_by_id[identity_key] = name
            if key in known and known[key] != value:
                raise ValueError(
                    f"Saige 项目中同名类别 {name} 的 {key} 不一致，无法安全映射类别"
                )
            known[key] = value

    for entry in project.get("classInfos") or []:
        collect_identity(entry)
    for file in project.get("projectFiles") or []:
        collect_identity(file)
        if isinstance(file, dict):
            for label in file.get("labelDataList") or []:
                collect_identity(label)

    values: list[str] = []
    seen_values: set[str] = set()
    for entry in project.get("classInfos") or []:
        name = entry.get("className")
        normalized = str(name) if name else ""
        if normalized and normalized not in seen_values:
            seen_values.add(normalized)
            values.append(normalized)
    if values:
        return values
    for file in project.get("projectFiles") or []:
        candidates = [file.get("className")]
        candidates.extend(label.get("className") for label in file.get("labelDataList") or [] if isinstance(label, dict))
        for name in candidates:
            normalized = str(name) if name else ""
            if normalized and normalized not in seen_values:
                if len(values) >= MAX_CLASSES:
                    raise ValueError(
                        f"Saige 项目超过 {MAX_CLASSES} 个类别的安全上限"
                    )
                seen_values.add(normalized)
                values.append(normalized)
    return values


def _validate_saige_structure(project: dict) -> None:
    class_infos = project.get("classInfos")
    if class_infos is not None and not isinstance(class_infos, list):
        raise ValueError("Saige 项目的 classInfos 必须是数组")
    if len(class_infos or []) > MAX_CLASSES:
        raise ValueError(f"Saige 项目超过 {MAX_CLASSES} 个类别定义的安全上限")
    for index, entry in enumerate(class_infos or []):
        if not isinstance(entry, dict):
            raise ValueError(f"Saige 项目的 classInfos[{index}] 必须是对象")
    project_files = project.get("projectFiles")
    if not isinstance(project_files, list):
        raise ValueError("Saige 项目的 projectFiles 必须是数组")
    if len(project_files) > MAX_DATASET_ITEMS:
        raise ValueError(f"Saige 项目超过 {MAX_DATASET_ITEMS} 个图像记录的安全上限")
    total_label_slots = 0
    for file_index, file in enumerate(project_files):
        if not isinstance(file, dict):
            raise ValueError(f"Saige 项目的 projectFiles[{file_index}] 必须是对象")
        labels = file.get("labelDataList")
        if labels is not None and not isinstance(labels, list):
            raise ValueError(
                f"Saige 项目的 projectFiles[{file_index}].labelDataList 必须是数组"
            )
        total_label_slots += len(labels or [])
        if total_label_slots > MAX_DATASET_ITEMS:
            raise ValueError(
                f"Saige 项目超过 {MAX_DATASET_ITEMS} 个标注记录的安全上限"
            )
        for label_index, label in enumerate(labels or []):
            if label is not None and not isinstance(label, dict):
                raise ValueError(
                    f"Saige 标注 projectFiles[{file_index}].labelDataList[{label_index}] "
                    "必须是对象或 null"
                )


def _load_saige_project(project: dict, *, name: str, source: Path, source_hash: str,
                        source_type: str, archive_entries: set[str] | None = None) -> Dataset:
    _validate_saige_structure(project)
    classes = _json_classes(project)
    class_indices = {label: index for index, label in enumerate(classes)}
    declared_classes = {
        str(entry["className"]) for entry in project.get("classInfos") or []
        if entry.get("className")
    }
    items: list[ReviewItem] = []
    project_type = str(project.get("projectType") or "unknown").lower()
    archive_lookup: dict[str, str] = {}
    if archive_entries:
        if len(archive_entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(
                f"visionproj 超过 {MAX_ARCHIVE_ENTRIES} 个归档条目的安全上限"
            )
        for entry in sorted(archive_entries):
            normalized = entry.replace("\\", "/")
            if normalized in archive_lookup and archive_lookup[normalized] != entry:
                raise ValueError(f"visionproj 包含冲突的归档路径：{normalized}")
            archive_lookup[normalized] = entry
    for file_index, file in enumerate(project.get("projectFiles") or []):
        path = str(file.get("filePath") or f"file_{file_index}")
        labels = [(index, label) for index, label in enumerate(file.get("labelDataList") or [])
                  if isinstance(label, dict)]
        # IAD/classification uses one image-level class; OCR/detection/segmentation use object labels.
        if (not labels and project_type in IMAGE_LEVEL_PROJECT_TYPES and
                file.get("className")):
            labels = [(0, {"className": file.get("className")})]
        if not labels:
            continue
        for label_no, label_data in labels:
            raw_label = label_data.get("className")
            if not raw_label and project_type == "ocr":
                raw_label = "OCR文本"
            if not raw_label:
                raise ValueError(
                    f"Saige 项目图像 {path} 的第 {label_no + 1} 个对象标注缺少 className"
                )
            label = str(raw_label)
            if (project_type != "ocr" and declared_classes and
                    label not in declared_classes):
                raise ValueError(f"Saige 对象标注引用了未声明类别：{label}")
            if label not in class_indices:
                if len(classes) >= MAX_CLASSES:
                    raise ValueError(
                        f"Saige 项目超过 {MAX_CLASSES} 个类别的安全上限"
                    )
                class_indices[label] = len(classes)
                classes.append(label)
            class_index = class_indices[label]
            item_id = str(len(items))
            x, y = _layout(len(items), class_index)
            normalized = path.replace("\\", "/")
            archive_name = archive_lookup.get(normalized)
            metadata = {"file_index": file_index, "label_no": label_no, "project_type": project_type,
                        "label_id": label_data.get("labelId"), "archive_entry": archive_name}
            for key in ("labelPosX", "labelPosY", "labelWidth", "labelHeight", "labelContour", "labelText"):
                if key in label_data:
                    metadata[key] = label_data[key]
            if all(key in label_data for key in ("labelPosX", "labelPosY", "labelWidth", "labelHeight")):
                px = _finite_float(label_data["labelPosX"], description="Saige 标注 X 坐标")
                py = _finite_float(label_data["labelPosY"], description="Saige 标注 Y 坐标")
                width = _finite_float(label_data["labelWidth"], description="Saige 标注宽度")
                height = _finite_float(label_data["labelHeight"], description="Saige 标注高度")
                metadata["bbox"] = [
                    px,
                    py,
                    _finite_add(px, width, description="Saige 标注右边界"),
                    _finite_add(py, height, description="Saige 标注下边界"),
                ]
            contour = label_data.get("labelContour")
            if isinstance(contour, str):
                try:
                    metadata["contours"] = strict_json_loads(contour)
                except json.JSONDecodeError:
                    pass
            if "bbox" not in metadata and "contours" in metadata:
                points = _contour_points(metadata["contours"])
                if points:
                    xs, ys = [point[0] for point in points], [point[1] for point in points]
                    metadata["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
            if len(items) >= MAX_DATASET_ITEMS:
                raise ValueError(
                    f"Saige 项目超过 {MAX_DATASET_ITEMS} 个复查项的安全上限"
                )
            items.append(ReviewItem(item_id, path, label, None, None, x, y,
                                    f"/api/preview/{item_id}", metadata=metadata))
    if not classes:
        classes.append("未分类")
    return Dataset(str(project.get("projectName") or name), source.resolve(), source_hash, classes, items,
                   source_type, metadata={"project_type": project_type})


def load_saige_json(path: Path) -> Dataset:
    path = path.resolve()
    payload, source_hash = _read_project_snapshot(path)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Saige 项目 JSON 必须使用 UTF-8 编码") from exc
    data = strict_json_loads(text)
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict) or "projectFiles" not in project:
        raise ValueError("不是可识别的 Saige 项目 JSON（缺少 project.projectFiles）")
    kind = str(project.get("projectType") or "unknown").lower()
    dataset = _load_saige_project(
        project,
        name=path.stem,
        source=path,
        source_hash=source_hash,
        source_type=f"saige-json:{kind}",
    )
    _assert_source_digest(path, source_hash)
    return dataset


def load_visionproj(path: Path) -> Dataset:
    path = path.resolve()
    with path.open("rb") as source_stream:
        before = os.fstat(source_stream.fileno())
        source_hash = _hash_stream(source_stream)
        _assert_same_file_stat(before, os.fstat(source_stream.fileno()), path)
        source_stream.seek(0)
        with zipfile.ZipFile(source_stream) as archive:
            all_infos = archive.infolist()
            if len(all_infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(
                    f"visionproj 超过 {MAX_ARCHIVE_ENTRIES} 个归档条目的安全上限"
                )
            if any(info.is_dir() and info.file_size for info in all_infos):
                raise ValueError("visionproj 包含带数据的目录条目，无法安全保真写回")
            infos = [info for info in all_infos if not info.is_dir()]
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("visionproj 包含重复的归档路径，已拒绝读取")
            entries = {info.filename for info in infos}
            manifests = sorted(
                (name for name in entries if name.lower().endswith(".json")),
                key=lambda name: (name.count("/") + name.count("\\"), len(name)),
            )
            if not manifests:
                raise ValueError("visionproj 中未找到项目 JSON")
            if len(manifests) > MAX_JSON_CANDIDATES:
                raise ValueError(
                    f"visionproj 超过 {MAX_JSON_CANDIDATES} 个 JSON 候选文件的安全上限"
                )
            readable_infos = [
                archive.getinfo(candidate)
                for candidate in manifests
                if archive.getinfo(candidate).file_size <= MAX_MANIFEST_BYTES
            ]
            if sum(info.file_size for info in readable_infos) > MAX_TOTAL_MANIFEST_BYTES:
                raise ValueError(
                    "visionproj 候选 JSON 的累计未压缩大小超过安全上限"
                )
            candidates: list[tuple[str, dict]] = []
            for candidate in manifests:
                info = archive.getinfo(candidate)
                if info.file_size > MAX_MANIFEST_BYTES:
                    continue
                with archive.open(info) as manifest_stream:
                    payload = manifest_stream.read(MAX_MANIFEST_BYTES + 1)
                if len(payload) > MAX_MANIFEST_BYTES:
                    continue
                try:
                    data = strict_json_loads(payload.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    continue
                candidate_project = data.get("project") if isinstance(data, dict) else None
                if isinstance(candidate_project, dict) and "projectFiles" in candidate_project:
                    candidates.append((candidate, candidate_project))
            if len(candidates) > 1:
                names = ", ".join(name for name, _project in candidates[:5])
                raise ValueError(f"visionproj 包含多个有效项目清单，无法安全选择：{names}")
            manifest, project = candidates[0] if candidates else ("", None)
            if not manifest or not isinstance(project, dict):
                raise ValueError("visionproj 内的 JSON 不是可识别的 Saige 项目")
            kind = str(project.get("projectType") or "unknown").lower()
            dataset = _load_saige_project(
                project,
                name=path.stem,
                source=path,
                source_hash=source_hash,
                source_type=f"visionproj:{kind}",
                archive_entries=entries,
            )
            dataset.metadata["manifest"] = manifest
        source_stream.seek(0)
        final_hash = _hash_stream(source_stream)
        after = os.fstat(source_stream.fileno())
    _assert_same_file_stat(before, after, path)
    _assert_same_file_stat(before, path.stat(), path)
    if not hmac.compare_digest(source_hash, final_hash):
        raise OSError(f"解析期间 visionproj 数据源发生变化：{path}")
    return dataset


def load_source(path: Path) -> Dataset:
    path = path.resolve()
    if path.is_dir():
        return load_folder(path)
    if path.suffix.lower() == ".srproj":
        return load_srproj(path)
    if path.suffix.lower() == ".json":
        return load_saige_json(path)
    if path.suffix.lower() == ".visionproj":
        return load_visionproj(path)
    raise ValueError(f"MVP 暂不支持此来源：{path.suffix or path}")


def read_preview(dataset: Dataset, item: ReviewItem) -> tuple[bytes, str] | None:
    archive_entry = item.metadata.get("archive_entry")
    if archive_entry and dataset.source and dataset.source.suffix.lower() == ".visionproj":
        archive_entry = str(archive_entry)
        if Path(archive_entry).suffix.lower() not in IMAGE_EXTENSIONS:
            return None
        with zipfile.ZipFile(dataset.source) as archive:
            try:
                info = archive.getinfo(str(archive_entry))
            except KeyError:
                return None
            if info.is_dir() or info.file_size > MAX_PREVIEW_BYTES:
                return None
            with archive.open(info) as stream:
                payload = stream.read(MAX_PREVIEW_BYTES + 1)
            if len(payload) > MAX_PREVIEW_BYTES:
                return None
        return payload, mimetypes.guess_type(archive_entry)[0] or "application/octet-stream"
    if not dataset.source:
        return None
    default_root = dataset.source if dataset.source.is_dir() else dataset.source.parent
    roots = [default_root.resolve()]
    dataset_metadata = getattr(dataset, "metadata", {}) or {}
    for configured in dataset_metadata.get("allowed_preview_roots") or []:
        root = Path(str(configured))
        if root.is_absolute():
            roots.append(root.resolve())
    try:
        raw_candidate = Path(item.relative_path)
        candidate = (raw_candidate if raw_candidate.is_absolute() else default_root / raw_candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    if not any(_is_within(candidate, root) for root in roots):
        raise PermissionError(
            f"图像路径位于未授权目录：{item.relative_path}；"
            "请在打开数据源时显式授权该图像根目录"
        )
    if candidate.is_file():
        try:
            before = candidate.stat()
            if before.st_size > MAX_PREVIEW_BYTES:
                return None
            with candidate.open("rb") as stream:
                payload = stream.read(MAX_PREVIEW_BYTES + 1)
            after = candidate.stat()
        except (OSError, PermissionError):
            return None
        if (len(payload) > MAX_PREVIEW_BYTES or
                (before.st_size, before.st_mtime_ns) !=
                (after.st_size, after.st_mtime_ns)):
            return None
        return payload, mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return None


def _contour_polygons(value, depth: int = 0) -> list[list[tuple[float, float]]]:
    if depth > 32 or not isinstance(value, (list, tuple)):
        return []
    if len(value) >= 3 and all(
        isinstance(point, (list, tuple)) and len(point) >= 2 and
        not isinstance(point[0], (list, tuple, dict)) and
        not isinstance(point[1], (list, tuple, dict))
        for point in value
    ):
        polygon = []
        for point in value:
            if any(isinstance(number, bool) for number in point[:2]):
                return []
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError, OverflowError):
                return []
            if not math.isfinite(x) or not math.isfinite(y):
                return []
            polygon.append((x, y))
        return [polygon]
    polygons: list[list[tuple[float, float]]] = []
    for child in value:
        polygons.extend(_contour_polygons(child, depth + 1))
    return polygons


def _contour_points(value) -> list[tuple[float, float]]:
    return [point for polygon in _contour_polygons(value) for point in polygon]


def render_preview(
        dataset: Dataset, item: ReviewItem, mode: str = "crop", *,
        show_contours: bool = True) -> tuple[bytes, str] | None:
    raw = read_preview(dataset, item)
    if not raw:
        return None
    try:
        from PIL import Image, ImageDraw
        with Image.open(io.BytesIO(raw[0])) as source:
            if (source.width <= 0 or source.height <= 0 or
                    source.width * source.height > MAX_IMAGE_PIXELS):
                return None
            if mode == "original":
                return raw
            image = source.convert("RGB")
        bbox = item.metadata.get("bbox")
        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = map(float, bbox)
            width, height = max(1, x2 - x1), max(1, y2 - y1)
            margin_x, margin_y = width * 0.35, height * 0.35
            crop_box = (max(0, int(x1 - margin_x)), max(0, int(y1 - margin_y)),
                        min(image.width, int(x2 + margin_x)), min(image.height, int(y2 + margin_y)))
            image = image.crop(crop_box)
            if show_contours:
                line_width = max(2, min(image.size) // 120)
                draw = ImageDraw.Draw(image)
                polygons = _contour_polygons(item.metadata.get("contours") or [])
                if polygons:
                    for polygon in polygons:
                        local = [(x - crop_box[0], y - crop_box[1]) for x, y in polygon]
                        draw.line(
                            local + [local[0]], fill=(255, 220, 0),
                            width=line_width, joint="curve"
                        )
                else:
                    draw.rectangle(
                        (x1 - crop_box[0], y1 - crop_box[1],
                         x2 - crop_box[0], y2 - crop_box[1]),
                        outline=(255, 220, 0), width=line_width,
                    )
        image.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, "JPEG", quality=88, optimize=True)
        return output.getvalue(), "image/jpeg"
    except (ImportError, OSError, ValueError, OverflowError, RecursionError):
        return raw
