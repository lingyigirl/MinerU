"""bbox 对齐算法 ISWM（IoU + Similarity Weighted Matcher）。

用 IoU（空间）+ 文本相似度（内容）加权打分，配合 Hungarian 全局最优匹配，
把 OCR 单元格（精确 bbox）对齐到 VLM 单元格（粗粒度/整页 bbox）。

核心公式（设计文档 §5.3）：
    match_score = α × IoU + (1-α) × text_similarity
    当任一 bbox 为整页 bbox 时 α→0（退化为纯文本匹配）

Hungarian 用纯 Python 实现（避免新增 scipy 依赖），
与 scipy.optimize.linear_sum_assignment 结果等价（见 tests/test_matcher.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mineru.utils.boxbase import calculate_iou
from mineru.utils.custom.matcher.text_similarity import text_similarity


@dataclass
class CellInfo:
    """统一的表格单元格表示。"""
    text: str
    bbox: list[float]              # [x0, y0, x1, y1]，像素坐标
    row_idx: int = -1
    col_idx: int = -1
    source: str = "unknown"        # "ocr" | "vlm" | "hybrid"
    confidence: float = 1.0


@dataclass
class CellMatch:
    """单元格匹配结果。"""
    ocr_cell: CellInfo
    vlm_cell: CellInfo
    bbox_iou: float                # IoU 分数
    text_sim: float                # 文本相似度
    combined_score: float          # 加权综合分
    matched_bbox: list[float]      # 融合 bbox（OCR bbox 优先）


@dataclass
class AlignmentResult:
    """对齐结果。"""
    matched_pairs: list[CellMatch] = field(default_factory=list)
    orphan_ocr: list[CellInfo] = field(default_factory=list)   # OCR 有但 VLM 无
    orphan_vlm: list[CellInfo] = field(default_factory=list)   # VLM 有但 OCR 无
    match_rate: float = 0.0


def _is_full_page_bbox(
    bbox: Optional[list[float]],
    page_size: Optional[list[float]] = None,
) -> bool:
    """判断 bbox 是否为退化/整页 bbox（IoU 无意义）。"""
    if bbox is None or len(bbox) != 4:
        return True
    x0, y0, x1, y1 = bbox
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return True
    if page_size is not None and len(page_size) >= 2:
        w, h = page_size[0], page_size[1]
        if w > 0 and h > 0:
            if x0 <= 0 and y0 <= 0 and x1 >= w * 0.98 and y1 >= h * 0.98:
                return True
    return False


def _linear_sum_assignment(cost: list[list[float]]) -> tuple[list[int], list[int]]:
    """纯 Python 匈牙利算法（最小权重二分图匹配，等价 scipy.linear_sum_assignment）。

    采用 e-maxx 的 O(n²m) 实现，支持矩形矩阵（行≠列）。

    Args:
        cost: N×M 成本矩阵（行=OCR，列=VLM）。

    Returns:
        (row_ind, col_ind) 匹配对索引列表，使总成本最小。
    """
    cost = [list(row) for row in cost]
    n = len(cost)
    if n == 0:
        return [], []
    m = len(cost[0])
    if m == 0:
        return [], []

    # 保证行数 ≤ 列数（否则转置）
    transpose = n > m
    if transpose:
        cost = list(map(list, zip(*cost)))
        n, m = m, n

    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    row_ind: list[int] = []
    col_ind: list[int] = []
    for j in range(1, m + 1):
        if p[j] != 0:
            r = p[j] - 1
            c = j - 1
            if transpose:
                row_ind.append(c)   # 转置后：原行 = 转置列
                col_ind.append(r)   # 原列 = 转置行
            else:
                row_ind.append(r)
                col_ind.append(c)
    return row_ind, col_ind


def _prefer_precise_bbox(ocr_bbox: list[float], vlm_bbox: list[float]) -> list[float]:
    """融合 bbox：OCR bbox 优先（更精确），OCR 退化时回退 VLM。"""
    if _is_full_page_bbox(ocr_bbox):
        return list(vlm_bbox)
    return list(ocr_bbox)


def align_table_cells(
    ocr_cells: list[CellInfo],
    vlm_cells: list[CellInfo],
    alpha: float = 0.6,
    min_match_score: float = 0.3,
    page_size: Optional[list[float]] = None,
) -> AlignmentResult:
    """IoU + OCR 相似度加权对齐（ISWM 算法）。

    使用 Hungarian 算法求解全局最优匹配。

    特殊处理：
    - 任一 bbox 为整页大小时自动设 alpha=0（纯文本模式）
    - 低于 min_match_score 的匹配视为孤儿

    Args:
        ocr_cells: OCR 提取的单元格列表。
        vlm_cells: VLM 识别的单元格列表。
        alpha: IoU 权重（0~1），1=纯空间，0=纯文本。
        min_match_score: 最低匹配分数阈值。
        page_size: 页面尺寸 [w, h]（用于识别整页 bbox，可选）。

    Returns:
        AlignmentResult 包含匹配对和孤儿单元格。
    """
    if not ocr_cells or not vlm_cells:
        return AlignmentResult(
            matched_pairs=[],
            orphan_ocr=list(ocr_cells),
            orphan_vlm=list(vlm_cells),
            match_rate=0.0,
        )

    n, m = len(ocr_cells), len(vlm_cells)
    cost = [[1.0] * m for _ in range(n)]
    scores = [[0.0] * m for _ in range(n)]
    ious = [[0.0] * m for _ in range(n)]
    sims = [[0.0] * m for _ in range(n)]

    for i in range(n):
        for j in range(m):
            ocr, vlm = ocr_cells[i], vlm_cells[j]
            iou = calculate_iou(ocr.bbox, vlm.bbox)
            sim = text_similarity(ocr.text, vlm.text)
            # 任一 bbox 为整页时退化为纯文本匹配
            a = alpha
            if _is_full_page_bbox(ocr.bbox, page_size) or _is_full_page_bbox(vlm.bbox, page_size):
                a = 0.0
            score = a * iou + (1.0 - a) * sim
            ious[i][j] = iou
            sims[i][j] = sim
            scores[i][j] = score
            cost[i][j] = 1.0 - score

    row_ind, col_ind = _linear_sum_assignment(cost)

    matched_pairs: list[CellMatch] = []
    matched_ocr: set[int] = set()
    matched_vlm: set[int] = set()
    for i, j in zip(row_ind, col_ind):
        if scores[i][j] < min_match_score:
            continue
        matched_pairs.append(CellMatch(
            ocr_cell=ocr_cells[i],
            vlm_cell=vlm_cells[j],
            bbox_iou=ious[i][j],
            text_sim=sims[i][j],
            combined_score=scores[i][j],
            matched_bbox=_prefer_precise_bbox(ocr_cells[i].bbox, vlm_cells[j].bbox),
        ))
        matched_ocr.add(i)
        matched_vlm.add(j)

    orphan_ocr = [c for i, c in enumerate(ocr_cells) if i not in matched_ocr]
    orphan_vlm = [c for i, c in enumerate(vlm_cells) if i not in matched_vlm]
    match_rate = len(matched_pairs) / max(n, m) if max(n, m) > 0 else 0.0

    return AlignmentResult(
        matched_pairs=matched_pairs,
        orphan_ocr=orphan_ocr,
        orphan_vlm=orphan_vlm,
        match_rate=match_rate,
    )
