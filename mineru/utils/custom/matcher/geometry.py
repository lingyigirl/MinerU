"""bbox 几何共享工具。"""

from __future__ import annotations


def merge_bboxes(*bboxes: list[float]) -> list[float]:
    """合并多个 bbox 为最小包围盒（[x0, y0, x1, y1] 像素坐标）。"""
    if not bboxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]
