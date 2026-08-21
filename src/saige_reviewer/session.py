from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .domain import Dataset, ReviewStatus


VALID_STATUSES = frozenset({"pending", "correct", "modified", "uncertain", "skipped"})
VALID_ANALYSIS_STATES = frozenset({"not_analyzed", "analyzed", "demo"})
ANALYSIS_METADATA_KEYS = frozenset({
    "label_confidence",
    "neighbor_support",
    "_analysis_preview_sha256",
})
MAX_HISTORY = 2048


@dataclass(slots=True)
class Command:
    item_id: str
    before_label: str
    after_label: str
    before_status: ReviewStatus
    after_status: ReviewStatus


@dataclass(slots=True)
class ReviewMutation:
    operation: str
    command: Command
    previous_revision: int
    previous_redo: list[Command] | None = None
    dropped_undo: Command | None = None


@dataclass(slots=True)
class AnalysisMutation:
    previous_revision: int
    before: list[tuple]


class ReviewSession:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        self.undo_stack: list[Command] = []
        self.redo_stack: list[Command] = []
        self._items_by_id = {}
        for item in dataset.items:
            if item.id in self._items_by_id:
                raise ValueError(f"复查项 ID 重复：{item.id}")
            self._items_by_id[item.id] = item
        self._base_coordinates = {
            item.id: (float(item.x), float(item.y)) for item in dataset.items
        }
        self._status_counts: dict[str, int] = {}
        self._analysis_state_counts: dict[str, int] = {}
        self._changed_count = 0
        self._reviewed_ids: set[str] = set()
        self._recount_summary()
        self._revision = 0
        self._saved_revision: int | None = None
        self._saved_path: Path | None = None
        self.layout_hash = self._layout_hash()

    @classmethod
    def _identity_metadata(cls, value):
        if isinstance(value, dict):
            return {str(key): cls._identity_metadata(child) for key, child in
                    sorted(value.items(), key=lambda pair: str(pair[0]))
                    if str(key) not in ANALYSIS_METADATA_KEYS and
                    not str(key).startswith("analysis_")}
        if isinstance(value, (list, tuple)):
            return [cls._identity_metadata(child) for child in value]
        if isinstance(value, Path):
            return str(value)
        return value

    def _layout_hash(self) -> str:
        digest = hashlib.sha256()
        header = {"source_type": self.dataset.source_type, "classes": self.dataset.classes}
        digest.update(json.dumps(header, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), default=str).encode("utf-8"))
        for item in self.dataset.items:
            identity = {
                "id": item.id,
                "relative_path": item.relative_path,
                "original_label": item.original_label,
                "metadata": self._identity_metadata(item.metadata),
            }
            digest.update(b"\0")
            digest.update(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode("utf-8"))
        return digest.hexdigest()

    def _item(self, item_id: str):
        return self._items_by_id.get(item_id)

    def mark_dirty(self) -> None:
        """Mark human review state as needing persistence."""
        self._revision += 1

    def _recount_summary(self) -> None:
        self._status_counts = {status: 0 for status in VALID_STATUSES}
        self._analysis_state_counts = {state: 0 for state in VALID_ANALYSIS_STATES}
        self._changed_count = 0
        self._reviewed_ids = set()
        for item in self.dataset.items:
            self._status_counts[item.status] = self._status_counts.get(item.status, 0) + 1
            self._analysis_state_counts[item.analysis_state] = self._analysis_state_counts.get(
                item.analysis_state, 0) + 1
            self._changed_count += int(item.label != item.original_label)
            if item.label != item.original_label or item.status != "pending":
                self._reviewed_ids.add(item.id)

    def _set_review(self, item, label: str, status: ReviewStatus) -> None:
        was_changed = item.label != item.original_label
        self._status_counts[item.status] = self._status_counts.get(item.status, 0) - 1
        item.label, item.status = label, status
        self._status_counts[item.status] = self._status_counts.get(item.status, 0) + 1
        self._changed_count += int(item.label != item.original_label) - int(was_changed)
        if item.label != item.original_label or item.status != "pending":
            self._reviewed_ids.add(item.id)
        else:
            self._reviewed_ids.discard(item.id)

    def stage_update(self, item_id: str, *, label: str | None = None,
                     status: ReviewStatus | None = None) -> ReviewMutation | None:
        item = self._item(item_id)
        if item is None:
            raise ValueError("复查项不存在")
        target_label = label if label is not None else item.label
        target_status = status if status is not None else item.status
        if not isinstance(target_label, str) or target_label not in self.dataset.classes:
            raise ValueError("类别不存在")
        if not isinstance(target_status, str) or target_status not in VALID_STATUSES:
            raise ValueError("复查状态不存在")
        if target_label == item.label and target_status == item.status:
            return None
        command = Command(item.id, item.label, target_label, item.status, target_status)
        previous_revision = self._revision
        previous_redo = self.redo_stack
        self._set_review(item, target_label, target_status)
        self.undo_stack.append(command)
        dropped_undo = self.undo_stack.pop(0) if len(self.undo_stack) > MAX_HISTORY else None
        self.redo_stack = []
        self.mark_dirty()
        return ReviewMutation("update", command, previous_revision, previous_redo, dropped_undo)

    def update(self, item_id: str, *, label: str | None = None, status: ReviewStatus | None = None) -> bool:
        return self.stage_update(item_id, label=label, status=status) is not None

    def stage_undo(self) -> ReviewMutation | None:
        if not self.undo_stack:
            return None
        previous_revision = self._revision
        command = self.undo_stack.pop()
        item = self._item(command.item_id)
        if item is None:
            self.undo_stack.clear()
            self.redo_stack.clear()
            return None
        self._set_review(item, command.before_label, command.before_status)
        self.redo_stack.append(command)
        self.mark_dirty()
        return ReviewMutation("undo", command, previous_revision)

    def undo(self) -> str | None:
        mutation = self.stage_undo()
        return mutation.command.item_id if mutation else None

    def stage_redo(self) -> ReviewMutation | None:
        if not self.redo_stack:
            return None
        previous_revision = self._revision
        command = self.redo_stack.pop()
        item = self._item(command.item_id)
        if item is None:
            self.undo_stack.clear()
            self.redo_stack.clear()
            return None
        self._set_review(item, command.after_label, command.after_status)
        self.undo_stack.append(command)
        dropped_undo = self.undo_stack.pop(0) if len(self.undo_stack) > MAX_HISTORY else None
        self.mark_dirty()
        return ReviewMutation("redo", command, previous_revision, dropped_undo=dropped_undo)

    def redo(self) -> str | None:
        mutation = self.stage_redo()
        return mutation.command.item_id if mutation else None

    def rollback_review(self, mutation: ReviewMutation) -> None:
        command = mutation.command
        item = self._item(command.item_id)
        if item is None:
            raise RuntimeError("无法回滚复查操作：复查项不存在")
        if mutation.operation == "update":
            if not self.undo_stack or self.undo_stack[-1] is not command:
                raise RuntimeError("无法回滚复查操作：历史已变化")
            self.undo_stack.pop()
            if mutation.dropped_undo is not None:
                self.undo_stack.insert(0, mutation.dropped_undo)
            self._set_review(item, command.before_label, command.before_status)
            self.redo_stack = mutation.previous_redo if mutation.previous_redo is not None else []
        elif mutation.operation == "undo":
            if not self.redo_stack or self.redo_stack[-1] is not command:
                raise RuntimeError("无法回滚撤销操作：历史已变化")
            self.redo_stack.pop()
            self._set_review(item, command.after_label, command.after_status)
            self.undo_stack.append(command)
        elif mutation.operation == "redo":
            if not self.undo_stack or self.undo_stack[-1] is not command:
                raise RuntimeError("无法回滚重做操作：历史已变化")
            self.undo_stack.pop()
            if mutation.dropped_undo is not None:
                self.undo_stack.insert(0, mutation.dropped_undo)
            self._set_review(item, command.before_label, command.before_status)
            self.redo_stack.append(command)
        else:
            raise RuntimeError("无法回滚未知复查操作")
        self._revision = mutation.previous_revision

    @staticmethod
    def _number(value, *, minimum: float | None = None, maximum: float | None = None) -> float:
        if isinstance(value, bool):
            raise ValueError("数值字段无效")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("数值字段必须是有限值")
        if minimum is not None and result < minimum:
            raise ValueError("数值字段超出范围")
        if maximum is not None and result > maximum:
            raise ValueError("数值字段超出范围")
        return result

    def stage_analysis_updates(self, updates) -> AnalysisMutation:
        """Validate a complete analysis result, then apply it as one atomic in-memory change."""
        if not isinstance(updates, list):
            raise ValueError("分析结果缺少样本更新")
        staged: dict[str, tuple[float, float, float, str, float, float, str | None]] = {}
        source_type = str(self.dataset.source_type)
        requires_preview_digest = (
            source_type.startswith("srproj:") or source_type.startswith("saige-json:")
        )
        for entry in updates:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ValueError("分析结果格式无效")
            item_id = str(entry["id"])
            if item_id not in self._items_by_id or item_id in staged:
                raise ValueError(f"分析结果包含无效或重复 ID：{item_id}")
            suggestion = entry.get("suggested_label")
            if suggestion not in self.dataset.classes:
                raise ValueError(f"分析建议类别无效：{suggestion}")
            preview_digest = entry.get("_analysis_preview_sha256")
            if preview_digest is not None and (
                not isinstance(preview_digest, str) or len(preview_digest) != 64 or
                any(character not in "0123456789abcdef" for character in preview_digest)
            ):
                raise ValueError("分析预览快照指纹无效")
            if requires_preview_digest and preview_digest is None:
                raise ValueError("分析结果缺少外部图像快照指纹")
            staged[item_id] = (
                self._number(entry.get("x")),
                self._number(entry.get("y")),
                self._number(entry.get("suspicion_score"), minimum=0, maximum=100),
                suggestion,
                self._number(entry.get("label_confidence"), minimum=0, maximum=1),
                self._number(entry.get("neighbor_support"), minimum=0, maximum=1),
                preview_digest,
            )
        if set(staged) != set(self._items_by_id):
            raise ValueError("分析结果不完整，未应用任何更新")
        before = []
        for item_id in staged:
            item = self._items_by_id[item_id]
            before.append((item, item.x, item.y, item.suspicion_score, item.suggested_label,
                           item.analysis_state, {key: item.metadata[key] for key in
                                                 ANALYSIS_METADATA_KEYS
                                                 if key in item.metadata}))
        mutation = AnalysisMutation(self._revision, before)
        try:
            for item_id, values in staged.items():
                item = self._items_by_id[item_id]
                (item.x, item.y, item.suspicion_score, item.suggested_label,
                 confidence, support, preview_digest) = values
                self._analysis_state_counts[item.analysis_state] = self._analysis_state_counts.get(
                    item.analysis_state, 0) - 1
                item.analysis_state = "analyzed"
                self._analysis_state_counts["analyzed"] = self._analysis_state_counts.get("analyzed", 0) + 1
                item.metadata["label_confidence"] = confidence
                item.metadata["neighbor_support"] = support
                if preview_digest is None:
                    item.metadata.pop("_analysis_preview_sha256", None)
                else:
                    item.metadata["_analysis_preview_sha256"] = preview_digest
        except Exception:
            self.rollback_analysis(mutation)
            raise
        return mutation

    def apply_analysis_updates(self, updates) -> int:
        return len(self.stage_analysis_updates(updates).before)

    def rollback_analysis(self, mutation: AnalysisMutation) -> None:
        for item, x, y, score, suggestion, analysis_state, metadata in mutation.before:
            self._analysis_state_counts[item.analysis_state] = self._analysis_state_counts.get(
                item.analysis_state, 0) - 1
            item.x, item.y = x, y
            item.suspicion_score, item.suggested_label = score, suggestion
            item.analysis_state = analysis_state
            self._analysis_state_counts[analysis_state] = self._analysis_state_counts.get(analysis_state, 0) + 1
            for key in ANALYSIS_METADATA_KEYS:
                item.metadata.pop(key, None)
            item.metadata.update(metadata)
        self._revision = mutation.previous_revision

    def clear_analysis_results(self) -> int:
        """Atomically invalidate runtime analysis while preserving human review state."""

        cleared = 0
        for item in self.dataset.items:
            had_analysis = (
                item.analysis_state == "analyzed" or item.suspicion_score is not None or
                item.suggested_label is not None or
                any(key in item.metadata for key in ANALYSIS_METADATA_KEYS)
            )
            if not had_analysis:
                continue
            self._analysis_state_counts[item.analysis_state] = self._analysis_state_counts.get(
                item.analysis_state, 0
            ) - 1
            item.x, item.y = self._base_coordinates[item.id]
            item.suspicion_score = None
            item.suggested_label = None
            item.analysis_state = "not_analyzed"
            self._analysis_state_counts["not_analyzed"] = self._analysis_state_counts.get(
                "not_analyzed", 0
            ) + 1
            for key in ANALYSIS_METADATA_KEYS:
                item.metadata.pop(key, None)
            cleared += 1
        return cleared

    def summary_payload(self) -> dict:
        return {
            "can_undo": bool(self.undo_stack),
            "can_redo": bool(self.redo_stack),
            "status_counts": dict(self._status_counts),
            "analysis_state_counts": dict(self._analysis_state_counts),
            "changed_count": self._changed_count,
        }

    def mutation_payload(self, item_id: str | None) -> dict:
        item = self._item(item_id) if item_id is not None else None
        patch = {"id": item.id, "label": item.label, "status": item.status} if item else None
        return {**self.summary_payload(), "item": patch}

    @staticmethod
    def _public_item(item) -> dict:
        metadata = {key: item.metadata[key] for key in ("label_confidence", "neighbor_support")
                    if key in item.metadata}
        return {
            "id": item.id,
            "relative_path": item.relative_path,
            "label": item.label,
            "suggested_label": item.suggested_label,
            "suspicion_score": item.suspicion_score,
            "x": item.x,
            "y": item.y,
            "preview_url": item.preview_url,
            "status": item.status,
            "original_label": item.original_label,
            "metadata": metadata,
            "analysis_state": item.analysis_state,
        }

    def payload(self) -> dict:
        return {
            "name": self.dataset.name,
            "source_type": self.dataset.source_type,
            "source": str(self.dataset.source) if self.dataset.source else None,
            "source_hash": self.dataset.source_hash,
            "allowed_preview_roots": list(self.dataset.metadata.get("allowed_preview_roots") or []),
            "classes": self.dataset.classes,
            "items": [self._public_item(item) for item in self.dataset.items],
            **self.summary_payload(),
            "score_notice": "疑似错标分数是复查优先级指标，不是真实错误概率。",
        }

    def export_payload(self) -> dict:
        changed = [self._public_item(self._items_by_id[item_id]) for item_id in sorted(self._reviewed_ids)]
        return {"format": "saige-review-session/v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "dataset": self.dataset.name, "source": str(self.dataset.source) if self.dataset.source else None,
                "source_hash": self.dataset.source_hash, "changes": changed}

    def save(self, path) -> bool:
        path = Path(path)
        resolved_path = path.resolve()
        if (self._saved_revision == self._revision and self._saved_path == resolved_path and path.exists()):
            return False
        payload = {
            "format": "saige-review-state/v2",
            "source": str(self.dataset.source) if self.dataset.source else None,
            "source_hash": self.dataset.source_hash,
            "layout_hash": self.layout_hash,
            # Analysis coordinates and scores are runtime/cache data, never session state.
            "items": {item_id: {"label": self._items_by_id[item_id].label,
                                "status": self._items_by_id[item_id].status}
                      for item_id in sorted(self._reviewed_ids)},
            "undo": [asdict(command) for command in self.undo_stack],
            "redo": [asdict(command) for command in self.redo_stack],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self._saved_revision = self._revision
        self._saved_path = resolved_path
        return True

    def _parse_commands(self, value) -> list[Command] | None:
        if not isinstance(value, list) or len(value) > MAX_HISTORY:
            return None
        commands: list[Command] = []
        required = {"item_id", "before_label", "after_label", "before_status", "after_status"}
        for entry in value:
            if not isinstance(entry, dict) or not required.issubset(entry):
                return None
            item_id = str(entry["item_id"])
            before_label, after_label = entry["before_label"], entry["after_label"]
            before_status, after_status = entry["before_status"], entry["after_status"]
            if (item_id not in self._items_by_id or before_label not in self.dataset.classes or
                    after_label not in self.dataset.classes or before_status not in VALID_STATUSES or
                    after_status not in VALID_STATUSES):
                return None
            commands.append(Command(item_id, before_label, after_label, before_status, after_status))
        return commands

    @staticmethod
    def _history_matches(items: dict[str, tuple[str, ReviewStatus]], undo: list[Command],
                         redo: list[Command]) -> bool:
        undo_state = dict(items)
        for command in reversed(undo):
            if undo_state.get(command.item_id) != (command.after_label, command.after_status):
                return False
            undo_state[command.item_id] = (command.before_label, command.before_status)
        redo_state = dict(items)
        for command in reversed(redo):
            if redo_state.get(command.item_id) != (command.before_label, command.before_status):
                return False
            redo_state[command.item_id] = (command.after_label, command.after_status)
        return True

    def restore(self, path) -> bool:
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("format") != "saige-review-state/v2":
                return False
            source = str(self.dataset.source) if self.dataset.source else None
            if (payload.get("source") != source or payload.get("source_hash") != self.dataset.source_hash or
                    payload.get("layout_hash") != self.layout_hash):
                return False
            saved_items = payload.get("items")
            if not isinstance(saved_items, dict):
                return False
            if not set(map(str, saved_items)).issubset(self._items_by_id):
                return False
            staged = {}
            for item in self.dataset.items:
                saved = saved_items.get(item.id)
                if saved is None:
                    continue
                if not isinstance(saved, dict):
                    return False
                label, status = saved.get("label"), saved.get("status", "pending")
                if label not in self.dataset.classes or status not in VALID_STATUSES:
                    return False
                # Deliberately ignore every legacy analysis field. Versioned analysis cache owns them.
                staged[item.id] = (label, status)
            item_states = {
                item.id: (staged.get(item.id, (item.label, item.status))[0],
                          staged.get(item.id, (item.label, item.status))[1])
                for item in self.dataset.items
            }
            undo = self._parse_commands(payload.get("undo", []))
            redo = self._parse_commands(payload.get("redo", []))
            if undo is None or redo is None or not self._history_matches(item_states, undo, redo):
                undo, redo = [], []
            for item_id, values in staged.items():
                item = self._items_by_id[item_id]
                item.label, item.status = values
            self.undo_stack = undo
            self.redo_stack = redo
            self._recount_summary()
            self._revision = 0
            self._saved_revision = 0
            self._saved_path = Path(path).resolve()
            return True
        except (OSError, ValueError, TypeError, OverflowError, RecursionError,
                json.JSONDecodeError):
            return False
