#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试表格内容列表生成（make_blocks_to_content_list 的 TABLE 分支）。

覆盖 特定文档类型优化规范.md 原则 11（回归测试多样性）：
验证 TABLE 段落块（含 table_body / table_caption / table_footnote 子块）
被正确转换为 content_list 格式：HTML 表格加图片前缀、标题/脚注文本提取、
bbox 归一化与 page_idx 注入。

历史说明：本文件原测试 make_table_body/make_table_caption/make_table_footnote
三个函数，上游 2.7.6 → 3.2.0 升级时被重构移除，改为统一由
make_blocks_to_content_list 处理 TABLE 块，故按现行 API 重写。
"""

import sys
import os

# 添加本地项目路径到系统路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.backend.vlm.vlm_middle_json_mkcontent import make_blocks_to_content_list
from mineru.utils.enum_class import BlockType, ContentType


def _table_para_block(html=None, image_path=None, caption=None, footnote=None):
    """构造一个 TABLE 段落块（含 body/caption/footnote 子块）。

    Args:
        html: 表格 body 的 HTML 字符串。
        image_path: 表格 body 的图片路径。
        caption: 表格标题文本。
        footnote: 表格脚注文本。

    Returns:
        TABLE 段落块字典。
    """
    blocks = []
    if html or image_path:
        span = {"type": ContentType.TABLE}
        if html:
            span["html"] = html
        if image_path:
            span["image_path"] = image_path
        blocks.append({"type": BlockType.TABLE_BODY, "lines": [{"spans": [span]}]})
    if caption:
        blocks.append({
            "type": BlockType.TABLE_CAPTION,
            "lines": [{"spans": [{"type": ContentType.TEXT, "content": caption}]}],
        })
    if footnote:
        blocks.append({
            "type": BlockType.TABLE_FOOTNOTE,
            "lines": [{"spans": [{"type": ContentType.TEXT, "content": footnote}]}],
        })
    return {
        "type": BlockType.TABLE,
        "bbox": [100, 200, 600, 500],
        "blocks": blocks,
    }


def test_table_body_with_image():
    """表格 body 含 HTML 与图片路径时，HTML 与加前缀的图片路径均被保留。"""
    img_buket_path = "https://example.com/images"
    para_block = _table_para_block(
        html="<table><tr><td>数据</td></tr></table>",
        image_path="table_images/table1.png",
    )

    result = make_blocks_to_content_list(para_block, img_buket_path, 0, [1000, 1000])

    assert result["type"] == ContentType.TABLE
    # 表格 HTML 存于 table_body 字段
    assert BlockType.TABLE_BODY in result
    assert "<table" in result[BlockType.TABLE_BODY]
    # 图片路径加前缀
    assert result["img_path"] == f"{img_buket_path}/table_images/table1.png"


def test_table_body_without_image():
    """表格 body 仅含 HTML 时，img_path 为空字符串而非图片路径。"""
    para_block = _table_para_block(html="<table><tr><td>数据</td></tr></table>")

    result = make_blocks_to_content_list(para_block, "https://example.com/images", 0, [1000, 1000])

    assert BlockType.TABLE_BODY in result
    assert result["img_path"] == ""


def test_table_caption_and_footnote():
    """表格标题与脚注文本被提取到对应列表字段。"""
    para_block = _table_para_block(
        html="<table><tr><td>数据</td></tr></table>",
        caption="表1：测试表格标题",
        footnote="注：本表格数据仅供参考",
    )

    result = make_blocks_to_content_list(para_block, "https://example.com/images", 0, [1000, 1000])

    assert result[BlockType.TABLE_CAPTION] == ["表1：测试表格标题"]
    assert result[BlockType.TABLE_FOOTNOTE] == ["注：本表格数据仅供参考"]


def test_table_bbox_and_page_idx():
    """bbox 被归一化到千分比坐标，page_idx 被注入。"""
    para_block = _table_para_block(html="<table><tr><td>数据</td></tr></table>")

    result = make_blocks_to_content_list(para_block, "https://example.com/images", 3, [1000, 1000])

    assert result["bbox"] == [100, 200, 600, 500]
    assert result["page_idx"] == 3


if __name__ == "__main__":
    test_table_body_with_image()
    test_table_body_without_image()
    test_table_caption_and_footnote()
    test_table_bbox_and_page_idx()

    print("=" * 50)
    print("所有测试完成！")
    print("=" * 50)
