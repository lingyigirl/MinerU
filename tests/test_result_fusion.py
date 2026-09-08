#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 Result Fusion 独立库（回归测试）。

覆盖设计文档 §6：
- deduplicate_spatial：空间去重（高 IoU+文本同→合并 / 文本不同→都保留 / 包含→并文本）
- resolve_field_conflicts：字段冲突（置信度投票 / 源优先级 / 多数投票）
- refine_vlm_bbox_with_ocr：整页 bbox → OCR 级细化
- fuse_results：融合主入口（去重 + 源贡献统计）
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.utils.custom.matcher import (
    SpatialBlock,
    FieldEntry,
    CellInfo,
    deduplicate_spatial,
    resolve_field_conflicts,
    refine_vlm_bbox_with_ocr,
    fuse_results,
)


def test_dedup_high_iou_high_sim_merges():
    a = SpatialBlock(text="户名 张三", bbox=[100, 100, 200, 130], source="kvp", confidence=0.9)
    b = SpatialBlock(text="户名 张三", bbox=[105, 102, 205, 132], source="mineru_native", confidence=0.8)
    result = deduplicate_spatial([a, b])
    assert len(result) == 1
    assert result[0].text == "户名 张三"            # 高置信度保留
    assert result[0].bbox == [100, 100, 205, 132]   # 并集 bbox


def test_dedup_high_iou_low_sim_keeps_both():
    a = SpatialBlock(text="张三", bbox=[100, 100, 200, 130], source="kvp", confidence=0.9)
    b = SpatialBlock(text="李四", bbox=[105, 102, 205, 132], source="kvp", confidence=0.8)
    result = deduplicate_spatial([a, b])
    assert len(result) == 2, "文本不同但位置重叠的 block 应都保留"


def test_dedup_containment_merges_text():
    whole = SpatialBlock(text="地址", bbox=[0, 0, 1000, 800], source="vlm_direct", confidence=0.7)
    inner = SpatialBlock(text="北京市朝阳区", bbox=[100, 100, 300, 130], source="kvp", confidence=0.9)
    result = deduplicate_spatial([whole, inner])
    assert len(result) == 1
    assert result[0].bbox == [0, 0, 1000, 800]      # 保留大者
    assert "地址" in result[0].text and "北京市朝阳区" in result[0].text


def test_field_conflict_confidence_voting():
    fields = [
        FieldEntry(key="户名", value="陈汉武", source="kvp", bbox=[100, 100, 200, 130], confidence=0.7),
        FieldEntry(key="户名", value="陆汉武", source="mineru_native", bbox=[100, 100, 200, 130], confidence=0.8),
        FieldEntry(key="户名", value="陈汉武", source="vlm_direct", bbox=[0, 0, 1000, 800], confidence=0.9),
    ]
    result = resolve_field_conflicts(fields, strategy="confidence_voting")
    assert len(result) == 1
    # kvp 置信度 0.7+0.15=0.85 > mineru 0.8、vlm_direct 0.9-0.1=0.8
    assert result[0].value == "陈汉武" and result[0].source == "kvp"


def test_field_conflict_source_priority():
    fields = [
        FieldEntry(key="户名", value="陈汉武", source="vlm_direct", bbox=[0, 0, 1000, 800], confidence=0.9),
        FieldEntry(key="户名", value="陆汉武", source="mineru_native", bbox=[100, 100, 200, 130], confidence=0.8),
    ]
    result = resolve_field_conflicts(fields, strategy="source_priority")
    assert result[0].source == "mineru_native", "source_priority 应选优先级更高的 mineru_native"


def test_field_conflict_majority_voting():
    fields = [
        FieldEntry(key="户名", value="陈汉武", source="kvp", bbox=[100, 100, 200, 130], confidence=0.7),
        FieldEntry(key="户名", value="陆汉武", source="mineru_native", bbox=[100, 100, 200, 130], confidence=0.8),
        FieldEntry(key="户名", value="陈汉武", source="vlm_direct", bbox=[0, 0, 1000, 800], confidence=0.9),
    ]
    result = resolve_field_conflicts(fields, strategy="majority_voting")
    assert result[0].value == "陈汉武", "多数投票应选出现 2 次的陈汉武"


def test_refine_vlm_bbox_with_ocr():
    vlm = SpatialBlock(text="户名 张三", bbox=[0, 0, 1000, 800], source="vlm_direct")
    ocr = [CellInfo(text="张三", bbox=[300, 500, 400, 540], source="ocr")]
    refined = refine_vlm_bbox_with_ocr(vlm, ocr)
    assert refined.bbox == [300, 500, 400, 540], "整页 bbox 应被 OCR 精确 bbox 替换"


def test_refine_vlm_bbox_no_match_keeps_original():
    vlm = SpatialBlock(text="户名 张三", bbox=[0, 0, 1000, 800], source="vlm_direct")
    ocr = [CellInfo(text="完全无关", bbox=[300, 500, 400, 540], source="ocr")]
    refined = refine_vlm_bbox_with_ocr(vlm, ocr)
    assert refined.bbox == [0, 0, 1000, 800], "无 OCR 匹配时保留原 bbox"


def test_fuse_results():
    mineru = [SpatialBlock(text="户名 张三", bbox=[100, 100, 200, 130], source="mineru_native", confidence=0.8)]
    kvp = [SpatialBlock(text="户名 张三", bbox=[105, 102, 205, 132], source="kvp", confidence=0.9)]
    result = fuse_results(mineru, kvp)
    assert len(result.unified_blocks) == 1
    assert result.source_stats == {"kvp": 1}, "去重后应保留高置信度的 kvp 源"


if __name__ == "__main__":
    test_dedup_high_iou_high_sim_merges()
    test_dedup_high_iou_low_sim_keeps_both()
    test_dedup_containment_merges_text()
    test_field_conflict_confidence_voting()
    test_field_conflict_source_priority()
    test_field_conflict_majority_voting()
    test_refine_vlm_bbox_with_ocr()
    test_refine_vlm_bbox_no_match_keeps_original()
    test_fuse_results()
    print("Result Fusion 回归测试全部通过")
