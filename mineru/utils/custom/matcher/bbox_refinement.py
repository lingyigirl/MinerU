"""bbox 层级融合：用 OCR 级精确 bbox 细化 VLM 的整页/粗粒度 bbox。"""

from __future__ import annotations

from mineru.utils.custom.matcher.geometry import merge_bboxes
from mineru.utils.custom.matcher.spatial_dedup import SpatialBlock
from mineru.utils.custom.matcher.text_similarity import text_similarity


def refine_vlm_bbox_with_ocr(
    vlm_block: SpatialBlock,
    ocr_cells: list,
    text_match_threshold: float = 0.8,
) -> SpatialBlock:
    """用 OCR 级精确 bbox 细化 VLM 的全页/粗粒度 bbox。

    场景：VLM 返回的 KVP 字段 bbox 为整页 [0, 0, W, H]，
    需要用 OCR 级精确 bbox 替换。

    处理流程：
    1. 在 OCR cells 中定位与 VLM 文本匹配的 cell（相似度 > 阈值 或子串包含）
    2. 若匹配到 → 用匹配 cells 的精确 bbox 并集替换 VLM bbox
    3. 若无匹配 → 保留 VLM 原始 bbox

    Args:
        vlm_block: VLM 产出的粗粒度 block。
        ocr_cells: OCR 级别的精确单元格列表（CellInfo）。
        text_match_threshold: 文本匹配阈值。

    Returns:
        细化后的 SpatialBlock（bbox 替换为精确坐标）。
    """
    if not ocr_cells:
        return vlm_block

    vlm_text = vlm_block.text
    matched_bboxes: list[list[float]] = []
    for cell in ocr_cells:
        cell_text = cell.text if isinstance(cell.text, str) else str(cell.text)
        if (
            text_similarity(cell_text, vlm_text) >= text_match_threshold
            or cell_text in vlm_text
            or vlm_text in cell_text
        ):
            matched_bboxes.append(list(cell.bbox))

    if not matched_bboxes:
        return vlm_block

    return SpatialBlock(
        text=vlm_block.text,
        bbox=merge_bboxes(*matched_bboxes),
        source=vlm_block.source,
        block_type=vlm_block.block_type,
        page_idx=vlm_block.page_idx,
        confidence=vlm_block.confidence,
    )
