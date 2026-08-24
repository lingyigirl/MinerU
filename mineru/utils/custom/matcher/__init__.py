"""bbox 对齐与多引擎结果融合工具包。

- text_similarity: OCR 容错的文本相似度（difflib + 数字加分）
- bbox_aligner: ISWM 对齐算法（IoU + 文本相似度 + Hungarian）
"""

from mineru.utils.custom.matcher.text_similarity import text_similarity
from mineru.utils.custom.matcher.bbox_aligner import (
    CellInfo,
    CellMatch,
    AlignmentResult,
    align_table_cells,
)

__all__ = [
    "text_similarity",
    "CellInfo",
    "CellMatch",
    "AlignmentResult",
    "align_table_cells",
]
