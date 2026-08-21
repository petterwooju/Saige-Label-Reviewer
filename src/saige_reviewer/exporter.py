from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from xml.parsers import expat

from .adapters import IMAGE_EXTENSIONS, load_source, source_fingerprint, strict_json_loads
from .session import ReviewSession


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalized(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _complete_fingerprint(path: Path) -> str:
    """Hash every file and empty directory in an export source.

    ``source_fingerprint`` is the dataset identity and can intentionally ignore
    unrelated files.  Backups have a stronger contract: every byte copied from
    a folder must be verified before the source can be replaced.
    """

    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if not path.is_dir():
        raise RuntimeError(f"导出路径不是文件或目录：{path}")
    entries = sorted(path.rglob("*"), key=lambda entry: entry.relative_to(path).as_posix())
    for entry in entries:
        relative = entry.relative_to(path).as_posix().encode("utf-8")
        if _is_link(entry):
            raise RuntimeError(f"为避免路径逃逸，目录导出不支持符号链接或目录联接：{entry}")
        if entry.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif entry.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise RuntimeError(f"目录中包含不支持的文件类型：{entry}")
    return digest.hexdigest()


def _source_changed_error() -> RuntimeError:
    return RuntimeError("检测到源数据被外部修改，已停止写出；请重新载入项目")


def _guard(session: ReviewSession) -> Path:
    source = session.dataset.source
    if source is None:
        raise ValueError("演示数据不能导出修正版项目")
    if not source.exists():
        raise RuntimeError("源项目已不存在，已停止写出")
    if source_fingerprint(source) != session.dataset.source_hash:
        raise _source_changed_error()
    return source


def _check_source_unchanged(session: ReviewSession, source: Path, complete_hash: str) -> None:
    if not source.exists():
        raise _source_changed_error()
    if source_fingerprint(source) != session.dataset.source_hash:
        raise _source_changed_error()
    if _complete_fingerprint(source) != complete_hash:
        raise _source_changed_error()


def _unique_path(parent: Path, name: str, *, suffix: str = "") -> Path:
    """Return a practically unique path and also handle a forced UUID collision."""

    for index in range(1000):
        collision = "" if index == 0 else f"-{index}"
        candidate = parent / f"{name}-{uuid.uuid4().hex}{collision}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为导出产物分配唯一路径：{parent}")


def _remove_quietly(path: Path) -> bool:
    """Best-effort cleanup; return whether a recovery copy remains."""

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return path.exists()
    return False


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    """Copy one file without ever following/overwriting a destination entry."""

    created = False
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            created = True
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        shutil.copystat(source, destination, follow_symlinks=False)
    except BaseException:
        if created:
            _remove_quietly(destination)
        raise


def _copy_directory_exclusive(source: Path, destination: Path, *, root_created=False) -> None:
    """Copy a tree using exclusive creation for every destination entry.

    ``copytree(..., dirs_exist_ok=True)`` can merge a concurrently inserted
    destination junction.  Every directory and file here must instead be newly
    claimed; an injected entry fails closed before any copy can target it.
    """

    if not root_created:
        destination.mkdir()
    for entry in sorted(source.iterdir(), key=lambda value: value.name):
        if _is_link(entry):
            raise RuntimeError(f"为避免路径逃逸，目录备份不支持符号链接或目录联接：{entry}")
        target = destination / entry.name
        if entry.is_dir():
            target.mkdir()
            _copy_directory_exclusive(entry, target, root_created=True)
            shutil.copystat(entry, target, follow_symlinks=False)
        elif entry.is_file():
            _copy_file_exclusive(entry, target)
        else:
            raise RuntimeError(f"目录中包含不支持的文件类型：{entry}")
    shutil.copystat(source, destination, follow_symlinks=False)


def _backup(source: Path, backup_root: Path, expected_source_hash: str | None) -> tuple[Path, str]:
    backup_root.mkdir(parents=True, exist_ok=True)
    before = _complete_fingerprint(source)
    if source_fingerprint(source) != expected_source_hash:
        raise _source_changed_error()
    destination = (
        _unique_path(backup_root, f"{_stamp()}_{source.stem}", suffix=source.suffix)
        if source.is_file()
        else _unique_path(backup_root, f"{_stamp()}_{source.name}")
    )
    owns_destination = False
    try:
        if source.is_dir():
            destination.mkdir()
            owns_destination = True
            _copy_directory_exclusive(source, destination, root_created=True)
        else:
            # Claim the name atomically.  UUIDs make a collision exceptionally
            # unlikely, but ``xb`` also prevents a race from overwriting an
            # independently-created file between allocation and copy.
            _copy_file_exclusive(source, destination)
            owns_destination = True
        after = _complete_fingerprint(source)
        copied = _complete_fingerprint(destination)
        if before != after or before != copied:
            raise RuntimeError("完整备份校验失败，已停止写出")
        if source_fingerprint(source) != expected_source_hash:
            raise _source_changed_error()
    except BaseException:
        if owns_destination:
            _remove_quietly(destination)
        raise
    return destination, before


def _xml_start_tag_end(payload: bytes, start: int) -> int:
    """Return the first byte after an XML start tag without parsing attributes."""

    quote: int | None = None
    for offset in range(start, len(payload)):
        value = payload[offset]
        if quote is not None:
            if value == quote:
                quote = None
        elif value in (ord("'"), ord('"')):
            quote = value
        elif value == ord(">"):
            return offset + 1
    raise RuntimeError("SRPROJ XML 起始标签不完整，已停止写出")


def _xml_numeric_span(content: bytes) -> tuple[int, int]:
    """Locate one decimal scalar while preserving comments/PIs byte-for-byte.

    Class index elements occasionally contain operator comments or processing
    instructions.  Re-serializing an ElementTree loses document-level lexical
    data and can move mixed-content tails.  This scanner accepts only whitespace,
    comments, PIs and exactly one decimal scalar (including inside CDATA), so a
    write can change just the scalar bytes and leave the entire XML untouched.
    """

    spans: list[tuple[int, int]] = []
    offset = 0
    while offset < len(content):
        value = content[offset]
        if value in b" \t\r\n":
            offset += 1
            continue
        if content.startswith(b"<!--", offset):
            end = content.find(b"-->", offset + 4)
            if end < 0:
                raise RuntimeError("SRPROJ 类别索引中的 XML 注释不完整")
            offset = end + 3
            continue
        if content.startswith(b"<?", offset):
            end = content.find(b"?>", offset + 2)
            if end < 0:
                raise RuntimeError("SRPROJ 类别索引中的处理指令不完整")
            offset = end + 2
            continue
        if content.startswith(b"<![CDATA[", offset):
            end = content.find(b"]]>", offset + 9)
            if end < 0:
                raise RuntimeError("SRPROJ 类别索引中的 CDATA 不完整")
            inner_start = offset + 9
            inner = content[inner_start:end]
            stripped = inner.strip(b" \t\r\n")
            number_start = inner_start + len(inner) - len(inner.lstrip(b" \t\r\n"))
            if not stripped or not stripped.isdigit():
                raise RuntimeError("SRPROJ 类别索引 CDATA 必须是非负十进制整数")
            spans.append((number_start, number_start + len(stripped)))
            offset = end + 3
            continue
        if ord("0") <= value <= ord("9"):
            end = offset + 1
            while end < len(content) and ord("0") <= content[end] <= ord("9"):
                end += 1
            spans.append((offset, end))
            offset = end
            continue
        raise RuntimeError("SRPROJ 类别索引包含不支持的混合内容，已停止写出")
    if len(spans) != 1:
        raise RuntimeError("SRPROJ 类别索引必须且只能包含一个非负十进制整数")
    return spans[0]


def _srproj_class_spans(payload: bytes) -> dict[tuple[int, int | None], tuple[int, int]]:
    """Map stable image/label ordinals to raw class-index byte spans."""

    declaration = payload[:256].lower().replace(b"_", b"-")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")) or any(
        marker in declaration for marker in (b'encoding="utf-16', b"encoding='utf-16",
                                             b'encoding="utf-32', b"encoding='utf-32")
    ):
        raise RuntimeError("SRPROJ 写回暂不支持 UTF-16/UTF-32 XML，请先另存为 UTF-8")

    parser = expat.ParserCreate()
    stack: list[str] = []
    image_no = -1
    current_image: int | None = None
    label_no = -1
    current_label: int | None = None
    active: dict[int, tuple[tuple[int, int | None], int]] = {}
    spans: dict[tuple[int, int | None], tuple[int, int]] = {}

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal image_no, current_image, label_no, current_label
        parent = stack[-1] if stack else None
        stack.append(name)
        if name == "Image" and parent == "ImageGroup":
            image_no += 1
            current_image = image_no
            label_no = -1
            current_label = None
        elif name == "Label" and parent == "LabelGroup" and current_image is not None:
            label_no += 1
            current_label = label_no

        key: tuple[int, int | None] | None = None
        if name == "ClassIndexOfLabel" and parent == "Image" and current_image is not None:
            key = (current_image, None)
        elif name == "ClassIndex" and parent == "Label" and current_image is not None:
            key = (current_image, current_label)
        if key is not None:
            depth = len(stack)
            if depth in active or key in spans:
                raise RuntimeError("SRPROJ 包含重复类别索引节点，已停止写出")
            active[depth] = (key, _xml_start_tag_end(payload, parser.CurrentByteIndex))

    def end(name: str) -> None:
        nonlocal current_image, current_label
        depth = len(stack)
        target = active.pop(depth, None)
        if target is not None:
            key, content_start = target
            content_end = parser.CurrentByteIndex
            if content_end < content_start:
                raise RuntimeError("SRPROJ 类别索引节点边界无效，已停止写出")
            relative_start, relative_end = _xml_numeric_span(payload[content_start:content_end])
            if key in spans:
                raise RuntimeError("SRPROJ 包含重复类别索引节点，已停止写出")
            spans[key] = (content_start + relative_start, content_start + relative_end)

        if name == "Label" and current_image is not None:
            current_label = None
        elif name == "Image" and current_image is not None:
            current_image = None
            current_label = None
        if not stack or stack[-1] != name:
            raise RuntimeError("SRPROJ XML 节点嵌套无效，已停止写出")
        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(payload, True)
    except RuntimeError:
        raise
    except expat.ExpatError as exc:
        raise RuntimeError(f"SRPROJ XML 无法安全解析：{exc}") from exc
    return spans


def _srproj_bytes(session: ReviewSession, source: Path) -> bytes:
    payload = source.read_bytes()
    spans = _srproj_class_spans(payload)
    class_indices = {label: index for index, label in enumerate(session.dataset.classes)}
    if len(class_indices) != len(session.dataset.classes):
        raise RuntimeError("导出校验失败，项目包含重复类别显示名")
    replacements: dict[tuple[int, int], bytes] = {}
    for item in session.dataset.items:
        if item.label == item.original_label:
            continue
        if item.label not in class_indices:
            raise RuntimeError(f"导出校验失败，类别不存在：{item.label}")
        image_no = item.metadata.get("image_no")
        if not isinstance(image_no, int) or isinstance(image_no, bool) or image_no < 0:
            raise RuntimeError(f"导出校验失败，图像索引无效：{item.relative_path}")
        label_no = item.metadata.get("label_no")
        if label_no is not None and (
            not isinstance(label_no, int) or isinstance(label_no, bool) or label_no < 0
        ):
            raise RuntimeError(f"导出校验失败，标注索引无效：{item.relative_path}")
        span = spans.get((image_no, label_no))
        if span is None:
            raise RuntimeError(f"导出校验失败，找不到标注节点：{item.relative_path}")
        if span in replacements:
            raise RuntimeError(f"导出校验失败，多个复查项指向同一标注：{item.relative_path}")
        replacements[span] = str(class_indices[item.label]).encode("ascii")

    pieces: list[bytes] = []
    cursor = 0
    for (start, end), replacement in sorted(replacements.items()):
        if start < cursor or end < start or end > len(payload):
            raise RuntimeError("SRPROJ 类别索引节点相互重叠或越界，已停止写出")
        pieces.extend((payload[cursor:start], replacement))
        cursor = end
    pieces.append(payload[cursor:])
    return b"".join(pieces)


def _saige_class_properties(project: dict) -> dict[str, dict]:
    properties: dict[str, dict] = {}
    names_by_id: dict[tuple[type, object], str] = {}

    def collect(node) -> None:
        if not isinstance(node, dict) or not node.get("className"):
            return
        name = str(node["className"])
        target = properties.setdefault(name, {})
        for key in ("classId", "classColor"):
            value = node.get(key)
            if value is None:
                continue
            if key == "classId":
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise RuntimeError(f"类别 {name} 的 classId 必须是字符串或整数")
                identity_key = (type(value), value)
                previous_name = names_by_id.get(identity_key)
                if previous_name is not None and previous_name != name:
                    raise RuntimeError(
                        f"classId {value} 同时属于类别 {previous_name} 和 {name}，已停止写出"
                    )
                names_by_id[identity_key] = name
            if key in target and target[key] != value:
                raise RuntimeError(f"类别 {name} 的 {key} 在源项目中不一致，已停止写出")
            target[key] = value

    for entry in project.get("classInfos") or []:
        collect(entry)
    for file in project.get("projectFiles") or []:
        collect(file)
        if isinstance(file, dict):
            for label in file.get("labelDataList") or []:
                collect(label)
    return properties


def _set_saige_class(node: dict, label: str, properties: dict[str, dict]) -> None:
    node["className"] = label
    values = properties.get(label, {})
    for key in ("classId", "classColor"):
        if key in values:
            node[key] = values[key]
        else:
            # Keeping an old class's ID/color next to the new name creates a
            # structurally inconsistent project.  Absence is safer and is
            # caught by downstream format validation if the field is required.
            node.pop(key, None)


def _saige_payload(session: ReviewSession, data: dict) -> bytes:
    project = data["project"]
    files = project.get("projectFiles") or []
    class_properties = _saige_class_properties(project)
    for item in session.dataset.items:
        if item.label == item.original_label:
            continue
        file_index = int(item.metadata["file_index"])
        label_no = int(item.metadata["label_no"])
        if not 0 <= file_index < len(files) or not isinstance(files[file_index], dict):
            raise RuntimeError(f"导出校验失败，项目文件索引无效：{file_index}")
        file = files[file_index]
        labels = file.get("labelDataList") or []
        if labels:
            if not 0 <= label_no < len(labels) or not isinstance(labels[label_no], dict):
                raise RuntimeError(f"导出校验失败，标注索引无效：{item.relative_path}#{label_no}")
            _set_saige_class(labels[label_no], item.label, class_properties)
        elif file.get("className") is not None:
            # Image-level IAD/classification records have no object label list.
            _set_saige_class(file, item.label, class_properties)
        else:
            raise RuntimeError(f"导出校验失败，找不到 Saige 标注节点：{item.relative_path}")
    return json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _json_bytes(session: ReviewSession, source: Path) -> bytes:
    return _saige_payload(session, strict_json_loads(source.read_text(encoding="utf-8-sig")))


def _write_visionproj(session: ReviewSession, source: Path, target: Path) -> None:
    manifest = session.dataset.metadata.get("manifest")
    if not manifest:
        raise RuntimeError("visionproj 清单信息缺失")
    with zipfile.ZipFile(source, "r") as incoming, zipfile.ZipFile(target, "w", allowZip64=True) as outgoing:
        outgoing.comment = incoming.comment
        with incoming.open(manifest) as stream:
            corrected_manifest = _saige_payload(
                session, strict_json_loads(stream.read().decode("utf-8-sig"))
            )
        for info in incoming.infolist():
            if info.is_dir():
                outgoing.writestr(info, b"")
            elif info.filename == manifest:
                outgoing.writestr(info, corrected_manifest)
            else:
                with incoming.open(info) as src, outgoing.open(info, "w") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    _validate_visionproj_copy(source, target, str(manifest))


def _zip_entry_sha256(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(info) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_visionproj_copy(source: Path, target: Path, manifest: str) -> None:
    """Prove that every non-manifest archive entry survived byte-for-byte."""

    with zipfile.ZipFile(source, "r") as incoming, zipfile.ZipFile(target, "r") as outgoing:
        source_infos = incoming.infolist()
        target_infos = outgoing.infolist()
        source_layout = [(info.filename, info.is_dir()) for info in source_infos]
        target_layout = [(info.filename, info.is_dir()) for info in target_infos]
        if source_layout != target_layout:
            raise RuntimeError("visionproj 修正版的归档条目清单与源项目不一致")
        if incoming.comment != outgoing.comment:
            raise RuntimeError("visionproj 修正版未保留源项目的归档注释")
        for source_info, target_info in zip(source_infos, target_infos, strict=True):
            metadata = ("date_time", "compress_type", "comment", "extra",
                        "internal_attr", "external_attr", "create_system")
            if any(getattr(source_info, key) != getattr(target_info, key) for key in metadata):
                raise RuntimeError(
                    f"visionproj 修正版未完整保留归档条目元数据：{source_info.filename}"
                )
            if source_info.filename == manifest:
                continue
            if (source_info.file_size != target_info.file_size or
                    _zip_entry_sha256(incoming, source_info) !=
                    _zip_entry_sha256(outgoing, target_info)):
                raise RuntimeError(
                    f"visionproj 修正版的归档条目内容与源项目不一致：{source_info.filename}"
                )


def _safe_relative(value: str, *, description: str) -> Path:
    candidate = Path(value)
    if (not candidate.parts or candidate.is_absolute() or candidate.anchor
            or any(part in ("", ".", "..") for part in candidate.parts)):
        raise RuntimeError(f"{description}包含不安全路径：{value}")
    return candidate


def _safe_class(label: str) -> str:
    path = _safe_relative(label, description="类别名")
    if len(path.parts) != 1 or path.name != label:
        raise RuntimeError(f"类别名必须是单层目录名：{label}")
    return label


def _folder_plan(session: ReviewSession) -> list[tuple[Path, Path]]:
    class_keys: set[str] = set()
    for label in session.dataset.classes:
        _safe_class(label)
        key = _normalized(label)
        if key in class_keys:
            raise RuntimeError(f"类别目录在当前文件系统中发生名称冲突：{label}")
        class_keys.add(key)

    source_keys: set[str] = set()
    target_keys: set[str] = set()
    plan: list[tuple[Path, Path]] = []
    for item in session.dataset.items:
        source_relative = _safe_relative(item.relative_path, description="源图像路径")
        _safe_class(item.original_label)
        _safe_class(item.label)
        if item.label not in session.dataset.classes:
            raise RuntimeError(f"标注引用了未知类别：{item.label}")
        if source_relative.parts[0] != item.original_label:
            raise RuntimeError(f"源图像路径与原始类别不一致：{item.relative_path}")
        remainder = Path(*source_relative.parts[1:])
        if not remainder.parts:
            raise RuntimeError(f"源图像路径缺少文件名：{item.relative_path}")
        target_relative = Path(item.label) / remainder
        source_key = _normalized(source_relative)
        target_key = _normalized(target_relative)
        if source_key in source_keys:
            raise RuntimeError(f"源数据包含重复图像路径：{source_relative}")
        if target_key in target_keys:
            raise RuntimeError(f"多个图像会写入同一目标路径：{target_relative}")
        source_keys.add(source_key)
        target_keys.add(target_key)
        plan.append((source_relative, target_relative))
    return plan


def _validate_folder(session: ReviewSession, target: Path) -> None:
    if not target.is_dir():
        raise RuntimeError(f"修正版目录不存在：{target}")
    plan = _folder_plan(session)
    expected = {_normalized(new): (new, item.label)
                for item, (_, new) in zip(session.dataset.items, plan, strict=True)}

    actual_classes = {_normalized(path.name): path.name for path in target.iterdir() if path.is_dir()}
    expected_classes = {_normalized(label): label for label in session.dataset.classes}
    if actual_classes != expected_classes:
        raise RuntimeError("修正版目录的类别目录与项目类别不一致")

    actual_files: dict[str, Path] = {}
    for path in target.rglob("*"):
        if _is_link(path):
            raise RuntimeError(f"修正版目录包含不安全链接：{path}")
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if not _is_within(path, target):
            raise RuntimeError(f"修正版图像路径逃逸目标目录：{path}")
        relative = path.relative_to(target)
        key = _normalized(relative)
        if key in actual_files:
            raise RuntimeError(f"修正版目录包含路径冲突：{relative}")
        actual_files[key] = relative
    if set(actual_files) != set(expected):
        missing = [str(expected[key][0]) for key in expected.keys() - actual_files.keys()]
        extra = [str(actual_files[key]) for key in actual_files.keys() - expected.keys()]
        detail = "; ".join(filter(None, (
            f"缺少 {', '.join(sorted(missing))}" if missing else "",
            f"多出 {', '.join(sorted(extra))}" if extra else "",
        )))
        raise RuntimeError(f"修正版目录图像清单校验失败：{detail}")

    checked = load_source(target)
    actual_labels = {_normalized(item.relative_path): item.label for item in checked.items}
    expected_labels = {key: label for key, (_, label) in expected.items()}
    if actual_labels != expected_labels:
        raise RuntimeError("修正版目录复读校验失败：类别或目标路径不一致")


def _validate(session: ReviewSession, target: Path) -> None:
    if session.dataset.source_type == "folder":
        _validate_folder(session, target)
        return
    if target.is_dir():
        raise RuntimeError("修正版格式校验失败：预期为项目文件")
    checked = load_source(target)
    expected = {item.id: item.label for item in session.dataset.items}
    actual = {item.id: item.label for item in checked.items}
    if actual != expected:
        raise RuntimeError("修正版复读校验失败：标注清单不一致")


def _apply_folder_plan(working: Path, plan: list[tuple[Path, Path]]) -> None:
    moves = [(source_relative, target_relative) for source_relative, target_relative in plan
             if _normalized(source_relative) != _normalized(target_relative)]
    if not moves:
        return
    holding = _unique_path(working.parent, f".{working.name}.moves")
    holding.mkdir()
    staged: list[tuple[Path, Path]] = []
    try:
        # Two phases make swaps (A/x -> B/x and B/x -> A/x) safe and ensure
        # failures only affect the disposable shadow copy.
        for index, (source_relative, target_relative) in enumerate(moves):
            old = working / source_relative
            if not _is_within(old, working) or not old.is_file():
                raise RuntimeError(f"源图像不存在或路径不安全：{source_relative}")
            temporary = holding / f"{index:08d}{old.suffix}"
            shutil.move(str(old), str(temporary))
            staged.append((temporary, target_relative))
        for temporary, target_relative in staged:
            new = working / target_relative
            if not _is_within(new, working):
                raise RuntimeError(f"目标图像路径逃逸导出目录：{target_relative}")
            new.parent.mkdir(parents=True, exist_ok=True)
            if new.exists():
                raise RuntimeError(f"目标类别中已有同名文件：{target_relative}")
            shutil.move(str(temporary), str(new))
    finally:
        _remove_quietly(holding)


def _restore_displaced(displaced: Path, source: Path, replacement_error: BaseException) -> None:
    """Restore a displaced source without overwriting a concurrently-created path."""

    if source.exists():
        raise RuntimeError(
            f"提交期间源路径被其他程序重新创建，未覆盖该路径；"
            f"原源数据保存在 {displaced}，请人工核对"
        ) from replacement_error
    try:
        os.rename(displaced, source)
    except BaseException as rollback_error:
        raise RuntimeError(
            f"替换失败且自动回滚失败；原源数据仍保存在 {displaced}，请勿删除备份"
        ) from rollback_error


def _commit_overwrite(session: ReviewSession, working: Path, source: Path,
                      complete_hash: str) -> Path:
    """Commit a validated replacement while preserving every late external write.

    Moving the source first closes the check/use gap: the exact filesystem
    object that will be displaced is fingerprinted after the atomic rename.
    The displaced object remains as a recovery copy because an already-open
    external handle can legally keep writing to it after the rename.
    """

    displaced = (_unique_path(source.parent, f".{source.stem}.original", suffix=source.suffix)
                 if source.is_file() else
                 _unique_path(source.parent, f".{source.name}.original"))
    os.rename(source, displaced)
    committed = False
    try:
        _check_source_unchanged(session, displaced, complete_hash)
        if source.exists():
            raise RuntimeError("提交期间源路径被其他程序重新创建")
        if working.is_dir():
            # On the supported Windows target os.rename is a no-clobber rename.
            os.rename(working, source)
            committed = True
        else:
            # A hard link is an atomic create-if-absent operation.  The staging
            # file is on the same volume as source for overwrite exports.
            try:
                os.link(working, source)
            except OSError as link_error:
                if os.name != "nt" or source.exists():
                    raise RuntimeError(
                        "当前文件系统不支持安全的无覆盖文件提交；原源数据已自动恢复"
                    ) from link_error
                # Windows rename is no-clobber and works on filesystems/SMB
                # shares that do not expose hard links.
                os.rename(working, source)
                committed = True
            else:
                committed = True
                _remove_quietly(working)
    except BaseException as replacement_error:
        if not committed:
            _restore_displaced(displaced, source, replacement_error)
        raise
    return displaced


def _export_folder(session: ReviewSession, source: Path, export_root: Path, *, overwrite: bool,
                   complete_hash: str) -> tuple[Path, str, Path | None]:
    plan = _folder_plan(session)
    working_parent = source.parent if overwrite else export_root
    working = _unique_path(working_parent, f".{source.name}.saige-staging")
    target = source if overwrite else _unique_path(export_root, f"{source.name}.corrected.{_stamp()}")
    recovery_copy: Path | None = None
    try:
        shutil.copytree(source, working, copy_function=shutil.copy2)
        if _complete_fingerprint(working) != complete_hash:
            raise RuntimeError("目录暂存副本校验失败，已停止写出")
        _check_source_unchanged(session, source, complete_hash)
        _apply_folder_plan(working, plan)
        _validate(session, working)
        _check_source_unchanged(session, source, complete_hash)
        output_hash = source_fingerprint(working)
        if overwrite:
            recovery_copy = _commit_overwrite(session, working, source, complete_hash)
        else:
            os.replace(working, target)
        return target, output_hash, recovery_copy
    finally:
        _remove_quietly(working)


def export_corrected(session: ReviewSession, workspace: Path, *, overwrite: bool = False) -> dict:
    source = _guard(session)
    changed = [item for item in session.dataset.items if item.label != item.original_label]
    if not changed:
        raise ValueError("没有暂存的类别修改")

    workspace = workspace.resolve(strict=False)
    if source.is_dir() and _is_within(workspace, source):
        raise ValueError("工作目录不能位于待导出的源图像目录内部")
    backup, complete_hash = _backup(source, workspace / "backups", session.dataset.source_hash)
    export_root = workspace / "exports"
    export_root.mkdir(parents=True, exist_ok=True)
    recovery_copy: Path | None = None

    if source.is_dir():
        target, output_hash, recovery_copy = _export_folder(
            session, source, export_root, overwrite=overwrite, complete_hash=complete_hash
        )
    else:
        suffix = source.suffix
        target = (source if overwrite else
                  _unique_path(export_root, f"{source.stem}.corrected.{_stamp()}", suffix=suffix))
        parent = source.parent if overwrite else export_root
        temporary = _unique_path(parent, f".{target.stem}.tmp", suffix=suffix)
        try:
            if suffix.lower() == ".srproj":
                temporary.write_bytes(_srproj_bytes(session, source))
            elif suffix.lower() == ".json":
                temporary.write_bytes(_json_bytes(session, source))
            elif suffix.lower() == ".visionproj":
                _write_visionproj(session, source, temporary)
            else:
                raise ValueError(f"暂不支持写出此格式：{suffix}")
            _validate(session, temporary)
            _check_source_unchanged(session, source, complete_hash)
            output_hash = source_fingerprint(temporary)
            if overwrite:
                recovery_copy = _commit_overwrite(session, temporary, source, complete_hash)
            else:
                os.replace(temporary, target)
        finally:
            _remove_quietly(temporary)

    result = {
        "output": str(target.resolve()),
        "backup": str(backup.resolve()),
        "overwritten": overwrite,
        "changed_count": len(changed),
        "output_hash": output_hash,
    }
    if recovery_copy is not None:
        result["recovery_copy"] = str(recovery_copy.resolve())
    return result
