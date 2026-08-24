#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 KVP 本地引擎新增的配对策略（回归测试）。

覆盖设计文档 §4 新增的三块：
- _merge_multiline_values：多行值聚合
- _pair_form_layout：策略 D（值在上、标签在下）
- _pair_same_row：策略 E（同行左右配对）
- _box_distance_v2：策略感知距离

以及 _pair_kvp 主控流程的冒号分隔回归（确认接线不破坏既有 A 策略）。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.utils.custom.kvp_local_engine import (
    _merge_multiline_values,
    _pair_form_layout,
    _pair_same_row,
    _pair_kvp,
    _box_distance_v2,
)
from mineru.utils.custom.labels import get_label_set


def _box(text, x1, y1, x2, y2, confidence=0.9):
    """构造一个 OCR box dict（含配对所需的全部字段）。"""
    return {
        "text": text,
        "bbox": (x1, y1, x2, y2),
        "confidence": confidence,
        "cx": (x1 + x2) / 2,
        "cy": (y1 + y2) / 2,
        "x1": x1, "x2": x2, "y1": y1, "y2": y2,
    }


def _label(text, x1, y1, x2, y2):
    """构造一个已匹配的标签 box。"""
    box = _box(text, x1, y1, x2, y2)
    box["matched_label"] = text
    return box


def test_merge_multiline_values():
    """垂直相邻且 X 高度重叠的两个值框应合并为一个（长字段换行）。"""
    values = [
        _box("广西桂林漓江农村合作", 100, 100, 300, 120),
        _box("银行建干支行", 100, 125, 280, 145),
    ]
    merged = _merge_multiline_values(values)
    assert len(merged) == 1, "高度重叠的相邻值框应合并"
    assert merged[0]["text"] == "广西桂林漓江农村合作 银行建干支行"
    # bbox 取并集
    assert merged[0]["x1"] == 100 and merged[0]["y1"] == 100
    assert merged[0]["x2"] == 300 and merged[0]["y2"] == 145


def test_merge_multiline_values_keeps_separate():
    """X 重叠不足（不同列）的值框不应合并。"""
    values = [
        _box("张三", 100, 100, 200, 120),
        _box("李四", 300, 125, 400, 145),
    ]
    merged = _merge_multiline_values(values)
    assert len(merged) == 2, "X 不重叠的值框不应合并"


def test_pair_form_layout_value_above():
    """策略 D：值在上、标签在下且 X 对齐 → 应配对。"""
    labels = [_label("摘要", 100, 200, 200, 220)]          # cy=210
    values = [_box("某公司转账", 100, 130, 300, 150)]       # cy=140，上方，X 重叠
    kvp, kvp_bboxes, paired = {}, {}, set()
    n = _pair_form_layout(labels, values, [], kvp, kvp_bboxes, paired)
    assert n == 1, "值在标签上方应配对"
    assert kvp["摘要"] == "某公司转账"
    assert 0 in paired, "配对的值框应标记为已占用"


def test_pair_form_layout_skips_value_below():
    """策略 D：值在标签下方（方向相反）→ 不应配对。"""
    labels = [_label("摘要", 100, 200, 200, 220)]
    values = [_box("某公司转账", 100, 260, 300, 280)]       # 下方
    kvp, kvp_bboxes, paired = {}, {}, set()
    n = _pair_form_layout(labels, values, [], kvp, kvp_bboxes, paired)
    assert n == 0 and "摘要" not in kvp, "值在标签下方不应由策略 D 配对"


def test_pair_same_row_label_left_value_right():
    """策略 E：同行、标签左值右 → 应配对。"""
    labels = [_label("甲方", 100, 200, 150, 220)]           # cy=210, x 100~150
    values = [_box("某公司", 160, 200, 300, 220)]           # 同行，右侧
    kvp, kvp_bboxes, paired = {}, {}, set()
    n = _pair_same_row(labels, values, [], kvp, kvp_bboxes, paired)
    assert n == 1, "同行标签左值右应配对"
    assert kvp["甲方"] == "某公司"


def test_pair_same_row_skips_value_left():
    """策略 E：值在标签左侧（方向相反）→ 不应配对。"""
    labels = [_label("甲方", 100, 200, 150, 220)]
    values = [_box("某公司", 40, 200, 90, 220)]             # 左侧
    kvp, kvp_bboxes, paired = {}, {}, set()
    n = _pair_same_row(labels, values, [], kvp, kvp_bboxes, paired)
    assert n == 0 and "甲方" not in kvp, "值在标签左侧不应由策略 E 配对"


def test_box_distance_v2_same_row_penalty():
    """_box_distance_v2 的 same_row 模式对垂直偏差超阈值重罚。"""
    a = _box("", 100, 100, 120, 120)
    b = _box("", 200, 100, 220, 120)    # 同行
    c = _box("", 200, 200, 220, 220)    # 垂直偏差 100px
    assert _box_distance_v2(a, b, mode="same_row") < _box_distance_v2(a, c, mode="same_row")


def test_pair_kvp_colon_regression():
    """_pair_kvp 主控流程的冒号分隔（策略 A）回归，且注入 _kvp_bboxes。"""
    boxes = [_box("户名：张三", 100, 100, 300, 120)]
    kvp = _pair_kvp(boxes, get_label_set("generic"))
    assert kvp.get("户名") == "张三", "冒号分隔应解析出户名=张三"
    assert "_kvp_bboxes" in kvp, "应注入 _kvp_bboxes"
    assert "户名" in kvp["_kvp_bboxes"], "户名字段应有 bbox 记录"


if __name__ == "__main__":
    test_merge_multiline_values()
    test_merge_multiline_values_keeps_separate()
    test_pair_form_layout_value_above()
    test_pair_form_layout_skips_value_below()
    test_pair_same_row_label_left_value_right()
    test_pair_same_row_skips_value_left()
    test_box_distance_v2_same_row_penalty()
    test_pair_kvp_colon_regression()
    print("KVP 本地引擎回归测试全部通过")
