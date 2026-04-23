#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 make_blocks_to_content_list_2 方法"""

import sys
import os

# 添加本地项目路径到系统路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.backend.vlm.vlm_middle_json_mkcontent import make_blocks_to_content_list_2
from mineru.utils.enum_class import BlockType, ContentType


def test_text_block():
    """测试文本块处理"""
    print("=== 测试文本块处理 ===\n")
    
    para_block = {
        "type": BlockType.TEXT,
        "bbox": [100, 200, 500, 300],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "这是一段普通文本"
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 1, "应该生成1个内容项"
    assert output_content[0]['type'] == BlockType.TEXT, "类型应该是TEXT"
    assert "这是一段普通文本" in output_content[0]['text'], "文本内容不正确"
    assert 'bbox' in output_content[0], "应该包含边界框"
    assert output_content[0]['page_idx'] == 0, "页面索引应该是0"
    
    print(f"✅ 文本块处理正确")
    print(f"   类型: {output_content[0]['type']}")
    print(f"   文本: {output_content[0]['text']}")
    print(f"   边界框: {output_content[0]['bbox']}\n")


def test_title_block():
    """测试标题块处理"""
    print("=== 测试标题块处理 ===\n")
    
    para_block = {
        "type": BlockType.TITLE,
        "bbox": [100, 100, 500, 150],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "第一章 绪论"
                    }
                ]
            }
        ],
        "level": 1
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 1, "应该生成1个内容项"
    assert output_content[0]['type'] == ContentType.TEXT, "类型应该是TEXT"
    assert "第一章 绪论" in output_content[0]['text'], "文本内容不正确"
    assert 'text_level' in output_content[0], "应该包含标题级别"
    
    print(f"✅ 标题块处理正确")
    print(f"   类型: {output_content[0]['type']}")
    print(f"   文本: {output_content[0]['text']}")
    print(f"   标题级别: {output_content[0].get('text_level', 'N/A')}\n")


def test_interline_equation_block():
    """测试行间公式块处理"""
    print("=== 测试行间公式块处理 ===\n")
    
    para_block = {
        "type": BlockType.INTERLINE_EQUATION,
        "bbox": [100, 200, 500, 300],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.INTERLINE_EQUATION,
                        "content": "E = mc^2"
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 1, "应该生成1个内容项"
    assert output_content[0]['type'] == ContentType.EQUATION, "类型应该是EQUATION"
    assert output_content[0]['text_format'] == 'latex', "格式应该是latex"
    assert "E = mc^2" in output_content[0]['text'], "公式内容不正确"
    
    print(f"✅ 行间公式块处理正确")
    print(f"   类型: {output_content[0]['type']}")
    print(f"   文本: {output_content[0]['text']}")
    print(f"   格式: {output_content[0]['text_format']}\n")


def test_image_block():
    """测试图片块处理"""
    print("=== 测试图片块处理 ===\n")
    
    para_block = {
        "type": BlockType.IMAGE,
        "bbox": [100, 200, 500, 400],
        "blocks": [
            {
                "type": BlockType.IMAGE_BODY,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.IMAGE,
                                "image_path": "images/figure1.png"
                            }
                        ]
                    }
                ]
            },
            {
                "type": BlockType.IMAGE_CAPTION,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "图1：示例图片"
                            }
                        ]
                    }
                ]
            },
            {
                "type": BlockType.IMAGE_FOOTNOTE,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "图片来源：网络"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 1, "应该生成1个内容项"
    assert output_content[0]['type'] == ContentType.IMAGE, "类型应该是IMAGE"
    assert output_content[0]['img_path'] == "https://example.com/images/images/figure1.png", "图片路径不正确"
    assert BlockType.IMAGE_CAPTION in output_content[0], "应该包含图片标题"
    assert BlockType.IMAGE_FOOTNOTE in output_content[0], "应该包含图片脚注"
    
    print(f"✅ 图片块处理正确")
    print(f"   类型: {output_content[0]['type']}")
    print(f"   图片路径: {output_content[0]['img_path']}")
    print(f"   标题: {output_content[0].get(BlockType.IMAGE_CAPTION, [])}")
    print(f"   脚注: {output_content[0].get(BlockType.IMAGE_FOOTNOTE, [])}\n")


def test_code_block():
    """测试代码块处理"""
    print("=== 测试代码块处理 ===\n")
    
    para_block = {
        "type": BlockType.CODE,
        "sub_type": BlockType.CODE,
        "guess_lang": "python",
        "bbox": [100, 200, 500, 400],
        "blocks": [
            {
                "type": BlockType.CODE_BODY,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "print('Hello World')"
                            }
                        ]
                    }
                ]
            },
            {
                "type": BlockType.CODE_CAPTION,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "代码示例1"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 1, "应该生成1个内容项"
    assert output_content[0]['type'] == BlockType.CODE, "类型应该是CODE"
    assert output_content[0]['sub_type'] == BlockType.CODE, "子类型应该是CODE"
    assert output_content[0]['guess_lang'] == "python", "语言猜测应该是python"
    assert BlockType.CODE_BODY in output_content[0], "应该包含代码主体"
    assert BlockType.CODE_CAPTION in output_content[0], "应该包含代码标题"
    
    print(f"✅ 代码块处理正确")
    print(f"   类型: {output_content[0]['type']}")
    print(f"   子类型: {output_content[0]['sub_type']}")
    print(f"   语言: {output_content[0].get('guess_lang', 'N/A')}")
    print(f"   代码: {output_content[0].get(BlockType.CODE_BODY, '')}")
    print(f"   标题: {output_content[0].get(BlockType.CODE_CAPTION, [])}\n")


def test_list_block():
    """测试列表块处理"""
    print("=== 测试列表块处理 ===\n")
    
    para_block = {
        "type": BlockType.LIST,
        "sub_type": "ordered",
        "bbox": [100, 200, 500, 400],
        "blocks": [
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "第一项"
                            }
                        ]
                    }
                ]
            },
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "第二项"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 2, "应该生成2个内容项"
    assert output_content[0]['type'] == BlockType.TEXT, "第一项类型应该是TEXT"
    assert output_content[1]['type'] == BlockType.TEXT, "第二项类型应该是TEXT"
    assert "第一项" in output_content[0]['text'], "第一项文本不正确"
    assert "第二项" in output_content[1]['text'], "第二项文本不正确"
    
    print(f"✅ 列表块处理正确")
    print(f"   生成项数: {len(output_content)}")
    print(f"   第一项: {output_content[0]['text']}")
    print(f"   第二项: {output_content[1]['text']}\n")


def test_table_block():
    """测试表格块处理"""
    print("=== 测试表格块处理 ===\n")
    
    para_block = {
        "type": BlockType.TABLE,
        "bbox": [100, 200, 500, 400],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "表格内容"
                    }
                ]
            }
        ],
        "blocks": [
            {
                "type": BlockType.TABLE_CAPTION,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "表1：示例表格"
                            }
                        ]
                    }
                ]
            },
            {
                "type": BlockType.TABLE_BODY,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TABLE,
                                "html": "<table><tr><td>数据</td></tr></table>"
                            }
                        ]
                    }
                ]
            },
            {
                "type": BlockType.TABLE_FOOTNOTE,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "注：表格数据"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_blocks_to_content_list_2(para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 3, "应该生成3个内容项"
    
    # 检查表格主体
    table_body = [item for item in output_content if item['type'] == BlockType.TABLE_BODY]
    assert len(table_body) == 1, "应该有1个表格主体"
    
    # 检查表格标题
    table_caption = [item for item in output_content if item['type'] == BlockType.TABLE_CAPTION]
    assert len(table_caption) == 1, "应该有1个表格标题"
    
    # 检查表格脚注
    table_footnote = [item for item in output_content if item['type'] == BlockType.TABLE_FOOTNOTE]
    assert len(table_footnote) == 1, "应该有1个表格脚注"
    
    print(f"✅ 表格块处理正确")
    print(f"   生成项数: {len(output_content)}")
    print(f"   表格主体: {table_body}")
    print(f"   表格标题: {table_caption[0]['type']}")
    print(f"   表格脚注: {table_footnote[0]['type']}\n")


if __name__ == "__main__":
    # 运行所有测试
    #test_text_block()
    #test_title_block()
    #test_interline_equation_block()
    #test_image_block()
    #test_code_block()
    #test_list_block()
    test_table_block()
    
    print("="*50)
    print("所有测试成功完成！")
    print("="*50)