"""多引擎结果融合主入口。"""

from __future__ import annotations

from dataclasses import dataclass, field

from mineru.utils.custom.matcher.bbox_refinement import refine_vlm_bbox_with_ocr
from mineru.utils.custom.matcher.spatial_dedup import (
    SpatialBlock,
    deduplicate_spatial,
)


@dataclass
class FusionConfig:
    """融合策略可调参数。"""
    iou_dedup_threshold: float = 0.7        # 空间去重 IoU 阈值
    text_overlap_threshold: float = 0.8     # 空间去重文本重叠阈值
    conflict_strategy: str = "confidence_voting"  # 字段冲突策略
    source_priority: list[str] = field(
        default_factory=lambda: ["kvp", "mineru_native", "vlm_direct"]
    )
    enable_bbox_refinement: bool = True     # 是否启用 bbox 细化
    alpha_iou: float = 0.6                  # 对齐算法 IoU 权重


@dataclass
class FusionResult:
    """融合结果。"""
    unified_blocks: list[SpatialBlock]      # 融合后的统一 block 列表
    conflict_log: list[dict] = field(default_factory=list)  # 冲突解决日志
    source_stats: dict[str, int] = field(default_factory=dict)  # 各源贡献统计


def fuse_results(
    mineru_native_blocks: list[SpatialBlock],
    kvp_blocks: list[SpatialBlock],
    vlm_direct_blocks: list[SpatialBlock] | None = None,
    ocr_cells: list | None = None,
    config: FusionConfig | None = None,
) -> FusionResult:
    """多引擎结果融合主入口。

    执行顺序：
    1. 空间去重（各源内部 + 源间）
    2. bbox 层级融合（VLM 全页 → OCR 级细化，可选）
    3. 生成统一 block 列表 + 各源贡献统计

    注：字段级冲突解决见 field_conflict.resolve_field_conflicts（独立函数，
    作用于 FieldEntry 列表，不在此处耦合 block 结构）。

    Args:
        mineru_native_blocks: MinerU 原生产出的 spatial blocks。
        kvp_blocks: KVP Pipeline 产出的 spatial blocks。
        vlm_direct_blocks: VLM 直接调用产出的 blocks（可选）。
        ocr_cells: OCR 级别的精确单元格（用于 bbox 细化，可选）。
        config: 融合策略配置。

    Returns:
        FusionResult 包含融合后的统一输出和统计信息。
    """
    config = config or FusionConfig()

    all_blocks: list[SpatialBlock] = []
    all_blocks.extend(mineru_native_blocks or [])
    all_blocks.extend(kvp_blocks or [])
    all_blocks.extend(vlm_direct_blocks or [])

    # 1. 空间去重（跨源）
    unified = deduplicate_spatial(
        all_blocks,
        iou_threshold=config.iou_dedup_threshold,
        text_overlap_threshold=config.text_overlap_threshold,
    )

    # 2. bbox 层级融合（VLM 整页 → OCR 级细化）
    if config.enable_bbox_refinement and ocr_cells:
        unified = [refine_vlm_bbox_with_ocr(b, ocr_cells) for b in unified]

    # 3. 统计各源贡献
    source_stats: dict[str, int] = {}
    for b in unified:
        source_stats[b.source] = source_stats.get(b.source, 0) + 1

    return FusionResult(
        unified_blocks=unified,
        conflict_log=[],
        source_stats=source_stats,
    )
