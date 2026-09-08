"""空间级去重：基于 bbox IoU 判断重复 block。"""

from __future__ import annotations

from dataclasses import dataclass

from mineru.utils.boxbase import calculate_iou, is_in
from mineru.utils.custom.matcher.geometry import merge_bboxes
from mineru.utils.custom.matcher.text_similarity import text_similarity


@dataclass
class SpatialBlock:
    """空间 block 表示。"""
    text: str
    bbox: list[float]              # [x0, y0, x1, y1]，像素坐标
    source: str                    # "mineru_native" | "kvp" | "vlm_direct"
    block_type: str = "text"       # "text" | "table" | "image" | "kvp_field"
    page_idx: int = 0
    confidence: float = 1.0


def deduplicate_spatial(
    blocks: list[SpatialBlock],
    iou_threshold: float = 0.7,
    text_overlap_threshold: float = 0.8,
) -> list[SpatialBlock]:
    """空间级去重。

    对同一页面上 IoU > threshold 的两个 block：

    1. 若文本相似度 > text_overlap_threshold：
       → 保留置信度高的，合并 bbox（取并集）
    2. 若文本相似度 < text_overlap_threshold：
       → 两者都保留（可能是同一区域的不同内容）
    3. 若一个完全包含另一个 (is_in)：
       → 保留大者，合并文本

    Args:
        blocks: 待去重的 block 列表。
        iou_threshold: IoU 判定阈值。
        text_overlap_threshold: 文本重叠判定阈值。

    Returns:
        去重后的 block 列表。
    """
    if len(blocks) <= 1:
        return list(blocks)

    # 按置信度降序排列，使高置信度 block 优先被保留
    ordered = sorted(blocks, key=lambda b: b.confidence, reverse=True)
    kept: list[SpatialBlock] = []

    for b in ordered:
        absorbed = False
        for i, k in enumerate(kept):
            if k.page_idx != b.page_idx:
                continue

            # 规则 3：完全包含 → 保留大者，合并文本。
            # 先于 IoU 判断，因为整页 bbox 包含小 bbox 时 IoU 很低。
            if is_in(b.bbox, k.bbox):
                k.text = k.text + "\n" + b.text
                absorbed = True
                break
            if is_in(k.bbox, b.bbox):
                kept[i] = SpatialBlock(
                    text=b.text + "\n" + k.text,
                    bbox=list(b.bbox),
                    source=b.source,
                    block_type=b.block_type,
                    page_idx=b.page_idx,
                    confidence=b.confidence,
                )
                absorbed = True
                break

            iou = calculate_iou(k.bbox, b.bbox)
            if iou <= iou_threshold:
                continue

            sim = text_similarity(k.text, b.text)

            # 规则 1：文本重叠 → 保留高置信度，合并 bbox
            if sim >= text_overlap_threshold:
                k.bbox = merge_bboxes(k.bbox, b.bbox)
                if b.confidence > k.confidence:
                    k.text = b.text
                    k.source = b.source
                    k.confidence = b.confidence
                absorbed = True
                break
            # 规则 2：文本不同 → 都保留（不吸收，继续检查下一个 kept）

        if not absorbed:
            kept.append(b)

    return kept
