from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

ReviewStatus = Literal["pending", "correct", "modified", "uncertain", "skipped"]
AnalysisState = Literal["not_analyzed", "analyzed", "demo"]


@dataclass(slots=True)
class ReviewItem:
    id: str
    relative_path: str
    label: str
    suggested_label: str | None
    suspicion_score: float | None
    x: float
    y: float
    preview_url: str = ""
    status: ReviewStatus = "pending"
    original_label: str = ""
    metadata: dict = field(default_factory=dict)
    analysis_state: AnalysisState = "not_analyzed"

    def __post_init__(self) -> None:
        if not self.original_label:
            self.original_label = self.label

    def public(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Dataset:
    name: str
    source: Path | None
    source_hash: str | None
    classes: list[str]
    items: list[ReviewItem]
    source_type: str
    metadata: dict = field(default_factory=dict)

    @property
    def changed_count(self) -> int:
        return sum(item.label != item.original_label for item in self.items)
