"""bbox 对齐与多引擎结果融合工具包。

- text_similarity: OCR 容错的文本相似度（difflib + 数字加分）
- bbox_aligner: ISWM 对齐算法（IoU + 文本相似度 + Hungarian）
- geometry: bbox 几何共享工具
- spatial_dedup: 空间级去重
- field_conflict: 字段级冲突解决
- bbox_refinement: VLM 整页 bbox → OCR 级细化
- result_fusion: 多引擎结果融合主入口
"""

from mineru.utils.custom.matcher.text_similarity import text_similarity
from mineru.utils.custom.matcher.bbox_aligner import (
    CellInfo,
    CellMatch,
    AlignmentResult,
    align_table_cells,
)
from mineru.utils.custom.matcher.geometry import merge_bboxes
from mineru.utils.custom.matcher.spatial_dedup import SpatialBlock, deduplicate_spatial
from mineru.utils.custom.matcher.field_conflict import FieldEntry, resolve_field_conflicts
from mineru.utils.custom.matcher.bbox_refinement import refine_vlm_bbox_with_ocr
from mineru.utils.custom.matcher.result_fusion import (
    FusionConfig,
    FusionResult,
    fuse_results,
)

__all__ = [
    "text_similarity",
    "CellInfo",
    "CellMatch",
    "AlignmentResult",
    "align_table_cells",
    "merge_bboxes",
    "SpatialBlock",
    "deduplicate_spatial",
    "FieldEntry",
    "resolve_field_conflicts",
    "refine_vlm_bbox_with_ocr",
    "FusionConfig",
    "FusionResult",
    "fuse_results",
]
