#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 bbox 对齐算法 ISWM 与文本相似度（回归测试）。

覆盖设计文档 §5：
- text_similarity：difflib 归一化 + 数字加分
- align_table_cells：IoU + 文本相似度 + Hungarian 全局最优
- 整页 bbox 退化（alpha→0，纯文本匹配）
- 低分匹配 → 孤儿

以及纯 Python Hungarian 与 scipy.optimize.linear_sum_assignment 的等价性
（环境已装 scipy 时对照总成本）。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.utils.custom.matcher import (
    text_similarity,
    CellInfo,
    align_table_cells,
)
from mineru.utils.custom.matcher.bbox_aligner import _linear_sum_assignment

try:
    from scipy.optimize import linear_sum_assignment as _scipy_lsa
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


def test_text_similarity_basic():
    assert text_similarity("张三", "张三") == 1.0
    assert text_similarity("帐号", "账号") > 0.0      # OCR 形近字
    assert text_similarity("¥700.00", "700.00元") > 0.5
    assert text_similarity("", "abc") == 0.0


def test_text_similarity_digit_bonus():
    # 相同数字串即使文本不同也应比不同数字更相似
    same_digit = text_similarity("金额 700.00", "金额 700.00 元")
    diff_digit = text_similarity("金额 700.00", "金额 900.00")
    assert same_digit > diff_digit


def test_align_basic():
    ocr = [
        CellInfo(text="张三", bbox=[100, 100, 200, 130], source="ocr"),
        CellInfo(text="李四", bbox=[100, 200, 200, 230], source="ocr"),
    ]
    vlm = [
        CellInfo(text="张三", bbox=[95, 95, 205, 135], source="vlm"),
        CellInfo(text="李四", bbox=[95, 195, 205, 235], source="vlm"),
    ]
    result = align_table_cells(ocr, vlm)
    assert len(result.matched_pairs) == 2
    assert result.orphan_ocr == [] and result.orphan_vlm == []
    by_text = {m.ocr_cell.text: m for m in result.matched_pairs}
    assert by_text["张三"].vlm_cell.text == "张三"
    assert by_text["李四"].vlm_cell.text == "李四"
    # matched_bbox 应取 OCR 精确 bbox
    assert by_text["张三"].matched_bbox == [100, 100, 200, 130]


def test_align_full_page_bbox_degrades_to_text():
    """VLM bbox 为整页时 alpha→0，退化为纯文本匹配，且取 OCR 精确 bbox。"""
    ocr = [CellInfo(text="户名 张三", bbox=[300, 500, 600, 560], source="ocr")]
    vlm = [CellInfo(text="户名 张三", bbox=[0, 0, 1000, 800], source="vlm")]
    result = align_table_cells(ocr, vlm, page_size=[1000, 800])
    assert len(result.matched_pairs) == 1
    m = result.matched_pairs[0]
    # alpha=0 → combined_score == 纯文本相似度
    assert abs(m.combined_score - text_similarity("户名 张三", "户名 张三")) < 1e-9
    # matched_bbox 取 OCR 精确 bbox（VLM 整页退化）
    assert m.matched_bbox == [300, 500, 600, 560]


def test_align_low_score_becomes_orphan():
    ocr = [CellInfo(text="张三", bbox=[100, 100, 200, 130], source="ocr")]
    vlm = [CellInfo(text="完全不同的文本XYZ", bbox=[400, 400, 500, 430], source="vlm")]
    result = align_table_cells(ocr, vlm, min_match_score=0.3)
    assert result.matched_pairs == []
    assert len(result.orphan_ocr) == 1 and len(result.orphan_vlm) == 1
    assert result.match_rate == 0.0


def test_align_rectangular_orphan():
    """OCR 多于 VLM 时，多出的 OCR 单元格应成为孤儿。"""
    ocr = [
        CellInfo(text="张三", bbox=[100, 100, 200, 130], source="ocr"),
        CellInfo(text="李四", bbox=[100, 200, 200, 230], source="ocr"),
        CellInfo(text="王五", bbox=[100, 300, 200, 330], source="ocr"),
    ]
    vlm = [CellInfo(text="张三", bbox=[95, 95, 205, 135], source="vlm")]
    result = align_table_cells(ocr, vlm)
    assert len(result.matched_pairs) == 1
    assert len(result.orphan_ocr) == 2 and result.orphan_vlm == []


def test_hungarian_matches_scipy():
    """纯 Python Hungarian 与 scipy 结果等价（总成本一致）。"""
    if not HAS_SCIPY:
        print("跳过 scipy 对照（环境未装 scipy）")
        return
    import random
    random.seed(42)
    for _ in range(300):
        n = random.randint(1, 6)
        m = random.randint(1, 6)
        cost = [[random.uniform(0, 1) for _ in range(m)] for _ in range(n)]
        my_rows, my_cols = _linear_sum_assignment(cost)
        sp_rows, sp_cols = _scipy_lsa(cost)
        my_total = sum(cost[i][j] for i, j in zip(my_rows, my_cols))
        sp_total = sum(cost[i][j] for i, j in zip(sp_rows, sp_cols))
        assert abs(my_total - sp_total) < 1e-6, (
            f"总成本不一致 n={n} m={m}: mine={my_total} scipy={sp_total}"
        )


if __name__ == "__main__":
    test_text_similarity_basic()
    test_text_similarity_digit_bonus()
    test_align_basic()
    test_align_full_page_bbox_degrades_to_text()
    test_align_low_score_becomes_orphan()
    test_align_rectangular_orphan()
    test_hungarian_matches_scipy()
    print("bbox 对齐 ISWM 回归测试全部通过")
