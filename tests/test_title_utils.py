#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试标题救援逻辑（回归测试）。

覆盖 特定文档类型优化规范.md 原则 1（输出不多不少）：
VLM 将居中短标题「资产负债表」误判为 header 而被 hybrid 转换丢弃时，
rescue_discarded_title_headers 应将其改判为 title 并移回正文，
同时不救援左对齐/跨页重复的 running header（页眉）。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.utils.custom.title_utils import rescue_discarded_title_headers


def _header_block(text, bbox, lines_count=1):
    """构造一个 header 块（lines 内含单个 span）。

    Args:
        text: span 文本。
        bbox: 块 bbox [x1, y1, x2, y2]。
        lines_count: line 数量（多行页眉用）。

    Returns:
        header 块字典。
    """
    lines = []
    if lines_count == 1:
        lines = [{
            "bbox": bbox,
            "spans": [{"bbox": bbox, "type": "text", "content": text}],
        }]
    else:
        # 多行：把文本按字符拆成多行（仅用于行数判定，内容不精确也无妨）
        per = max(1, len(text) // lines_count)
        lines = [
            {
                "bbox": bbox,
                "spans": [{"bbox": bbox, "type": "text", "content": text[i:i + per]}],
            }
            for i in range(0, len(text), per)
        ][:lines_count]
    return {"bbox": bbox, "type": "header", "angle": 0, "lines": lines, "index": 1}


def test_rescue_centered_title_header():
    """居中、单行、短文本的 header 应被救回 preproc_blocks 并转为 title（level 2）。"""
    page_w, page_h = 595, 842
    # 居中标题「资产负债表」：中心 x = (262+352)/2 = 307，偏离页中心 297.5 约 1.6%
    title_block = _header_block("资产负债表", [262, 58, 352, 75])
    pdf_info_list = [{
        "page_size": [page_w, page_h],
        "page_idx": 3,
        "preproc_blocks": [],
        "discarded_blocks": [title_block],
    }]

    rescue_discarded_title_headers(pdf_info_list)

    preproc = pdf_info_list[0]["preproc_blocks"]
    discarded = pdf_info_list[0]["discarded_blocks"]
    assert len(preproc) == 1, "居中短标题应被救回 preproc_blocks"
    assert preproc[0]["type"] == "title", "救回后类型应为 title"
    assert preproc[0]["level"] == 2, "救回后层级应为 level 2"
    assert discarded == [], "救回后应从 discarded_blocks 移除"


def test_rescue_skips_left_aligned_running_header():
    """左对齐的 header（页眉）不应被救回。"""
    page_w, page_h = 595, 842
    # 左对齐公司名：中心 x = (92+210)/2 = 151，偏离页中心 297.5 约 24.6%
    company_block = _header_block("成都矽半导体有限公司", [92, 37, 210, 97])
    pdf_info_list = [{
        "page_size": [page_w, page_h],
        "page_idx": 3,
        "preproc_blocks": [],
        "discarded_blocks": [company_block],
    }]

    rescue_discarded_title_headers(pdf_info_list)

    assert pdf_info_list[0]["preproc_blocks"] == [], "左对齐页眉不应被救回"
    assert len(pdf_info_list[0]["discarded_blocks"]) == 1, "左对齐页眉应留在 discarded_blocks"


def test_rescue_skips_repeated_running_header_across_pages():
    """跨页重复出现的同一 header 文本（running header）不应被救回。"""
    page_w, page_h = 595, 842
    # 居中但跨页重复的文本（如居中页眉）不应救
    title_block_page1 = _header_block("审计报告", [262, 40, 352, 55])
    title_block_page2 = _header_block("审计报告", [262, 40, 352, 55])
    pdf_info_list = [
        {
            "page_size": [page_w, page_h],
            "page_idx": 0,
            "preproc_blocks": [],
            "discarded_blocks": [title_block_page1],
        },
        {
            "page_size": [page_w, page_h],
            "page_idx": 1,
            "preproc_blocks": [],
            "discarded_blocks": [title_block_page2],
        },
    ]

    rescue_discarded_title_headers(pdf_info_list)

    assert pdf_info_list[0]["preproc_blocks"] == [], "跨页重复的居中 header 不应被救回"
    assert pdf_info_list[1]["preproc_blocks"] == [], "跨页重复的居中 header 不应被救回"
    assert len(pdf_info_list[0]["discarded_blocks"]) == 1, "重复页眉应留在 discarded_blocks"


def test_rescue_long_title_within_limit():
    """13 字居中标题「2023年12月份会计报表」应被救回（覆盖 20 字上限内的长标题）。"""
    page_w, page_h = 595, 842
    # 居中 13 字标题：中心 x = (180+415)/2 = 297.5 ≈ 页中心 297.5
    title_block = _header_block("2023年12月份会计报表", [180, 26, 415, 42])
    pdf_info_list = [{
        "page_size": [page_w, page_h],
        "page_idx": 0,
        "preproc_blocks": [],
        "discarded_blocks": [title_block],
    }]

    rescue_discarded_title_headers(pdf_info_list)

    preproc = pdf_info_list[0]["preproc_blocks"]
    discarded = pdf_info_list[0]["discarded_blocks"]
    assert len(preproc) == 1, "13 字居中标题应被救回"
    assert preproc[0]["type"] == "title", "救回后类型应为 title"
    assert preproc[0]["level"] == 2, "救回后层级应为 level 2"
    assert discarded == [], "救回后应从 discarded_blocks 移除"


def test_rescue_skips_overlong_header():
    """超过 20 字的居中单行 header（段落/页眉说明）不应被救回，锁死上限。"""
    page_w, page_h = 595, 842
    overlong_text = "超长标题" * 6  # 24 字，超过 _MAX_TITLE_LEN=20
    block = _header_block(overlong_text, [180, 26, 415, 42])  # 居中
    pdf_info_list = [{
        "page_size": [page_w, page_h],
        "page_idx": 0,
        "preproc_blocks": [],
        "discarded_blocks": [block],
    }]

    rescue_discarded_title_headers(pdf_info_list)

    assert pdf_info_list[0]["preproc_blocks"] == [], "超过 20 字的居中 header 不应被救回"
    assert len(pdf_info_list[0]["discarded_blocks"]) == 1, "超长 header 应留在 discarded_blocks"


if __name__ == "__main__":
    test_rescue_centered_title_header()
    test_rescue_skips_left_aligned_running_header()
    test_rescue_skips_repeated_running_header_across_pages()
    test_rescue_long_title_within_limit()
    test_rescue_skips_overlong_header()
    print("标题救援回归测试全部通过")
