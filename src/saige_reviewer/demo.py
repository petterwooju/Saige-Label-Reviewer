from __future__ import annotations

import math

from .domain import Dataset, ReviewItem


def make_empty() -> Dataset:
    """Return the initial product state before a local source is opened."""
    return Dataset("尚未打开数据源", None, None, [], [], "empty")


def make_demo() -> Dataset:
    classes = ["划痕", "污点", "缺口"]
    items = []
    for index in range(30):
        class_index = index % len(classes)
        label = classes[class_index]
        score = round(96 - index * 2.7, 1)
        suggested = classes[(class_index + 1) % len(classes)] if index % 5 == 0 else label
        angle = index * 1.73
        items.append(ReviewItem(str(index), f"demo/{label}/sample_{index + 1:03}.png", label, suggested,
                                max(5, score), class_index * 90 + math.cos(angle) * 35,
                                math.sin(angle) * 42, f"/api/preview/{index}", analysis_state="demo"))
    return Dataset("演示数据", None, None, classes, items, "demo")
