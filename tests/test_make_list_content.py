#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 make_list_content 方法"""

import sys
import os

# 添加本地项目路径到系统路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.backend.vlm.vlm_middle_json_mkcontent import make_list_content
from mineru.utils.enum_class import BlockType, ContentType


def test_make_list_content():
    """测试 make_list_content 方法"""
    
    # 模拟一个列表段落块
    para_block = {
        "type": BlockType.LIST,
        "sub_type": "ordered",  # 有序列表
        "bbox": [100, 200, 500, 400],  # 列表的边界框
        "blocks": [
            # 第一个列表项
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "第一项：这是列表的第一项内容"
                            }
                        ]
                    }
                ]
            },
            # 第二个列表项
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "第二项：这是列表的第二项内容"
                            }
                        ]
                    }
                ]
            },
            # 第三个列表项（包含内联公式）
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "第三项：包含公式 "
                            },
                            {
                                "type": ContentType.INLINE_EQUATION,
                                "content": "E=mc^2"
                            },
                            {
                                "type": ContentType.TEXT,
                                "content": " 的内容"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    # 模拟输出内容列表
    output_content = []
    
    # 模拟页面信息
    page_idx = 0
    page_size = [1000, 1200]  # 页面宽度和高度
    
    # 测试 make_list_content 方法
    print("=== 测试 make_list_content 方法 ===\n")
    print(f"输入参数:")
    print(f"  - 列表类型: {para_block['type']}")
    print(f"  - 子类型: {para_block['sub_type']}")
    print(f"  - 列表项数量: {len(para_block['blocks'])}")
    print(f"  - 页面索引: {page_idx}")
    print(f"  - 页面尺寸: {page_size}\n")
    
    # 调用方法
    make_list_content(BlockType.LIST, para_block, output_content, page_idx, page_size)
    
    # 输出结果
    print(f"输出结果:")
    print(f"  - 生成的列表项数量: {len(output_content)}\n")
    
    # 详细输出每个列表项
    for i, item in enumerate(output_content, 1):
        print(f"列表项 {i}:")
        print(f"  - 类型: {item['type']}")
        print(f"  - 文本: {item['text']}")
        if 'bbox' in item:
            print(f"  - 边界框: {item['bbox']}")
        print(f"  - 页面索引: {item['page_idx']}")
        print()
    
    # 验证结果
    print("=== 验证结果 ===")
    
    # 验证列表项数量
    assert len(output_content) == 3, f"期望生成3个列表项，实际生成{len(output_content)}个"
    print("✅ 列表项数量正确")
    
    # 验证每个列表项的类型
    for i, item in enumerate(output_content):
        assert item['type'] == BlockType.TEXT, f"列表项{i+1}的类型应该是TEXT"
    print("✅ 所有列表项类型正确")
    
    # 验证列表项文本内容
    assert "第一项" in output_content[0]['text'], "第一个列表项应该包含'第一项'"
    assert "第二项" in output_content[1]['text'], "第二个列表项应该包含'第二项'"
    assert "第三项" in output_content[2]['text'], "第三个列表项应该包含'第三项'"
    assert "E=mc^2" in output_content[2]['text'], "第三个列表项应该包含公式'E=mc^2'"
    print("✅ 列表项文本内容正确")
    
    # 验证边界框
    for i, item in enumerate(output_content):
        assert 'bbox' in item, f"列表项{i+1}应该包含边界框"
        assert 'page_idx' in item, f"列表项{i+1}应该包含页面索引"
        assert item['page_idx'] == page_idx, f"列表项{i+1}的页面索引应该是{page_idx}"
    print("✅ 边界框和页面索引正确")
    
    # 验证边界框计算
    expected_bbox = [
        int(100 * 1000 / 1000),  # x0
        int(200 * 1000 / 1200),  # y0
        int(500 * 1000 / 1000),  # x1
        int(400 * 1000 / 1200)   # y1
    ]
    assert output_content[0]['bbox'] == expected_bbox, f"边界框计算不正确，期望{expected_bbox}，实际{output_content[0]['bbox']}"
    print("✅ 边界框计算正确")
    
    print("\n=== 测试完成 ===")
    print("所有测试用例通过！")


def test_make_list_content_with_empty_blocks():
    """测试空的列表块"""
    
    para_block = {
        "type": BlockType.LIST,
        "sub_type": "unordered",
        "bbox": [100, 200, 500, 400],
        "blocks": []  # 空列表
    }
    
    output_content = []
    page_idx = 0
    page_size = [1000, 1200]
    
    print("\n=== 测试空列表块 ===")
    make_list_content(BlockType.LIST, para_block, output_content, page_idx, page_size)
    
    assert len(output_content) == 0, "空列表应该生成0个列表项"
    print("✅ 空列表处理正确")


def test_make_list_content_with_complex_spans():
    """测试包含复杂span的列表项"""
    
    para_block = {
        "type": BlockType.LIST,
        "sub_type": "unordered",
        "bbox": [100, 200, 500, 400],
        "blocks": [
            {
                "type": BlockType.TEXT,
                "lines": [
                    {
                        "spans": [
                            {
                                "type": ContentType.TEXT,
                                "content": "复杂列表项："
                            },
                            {
                                "type": ContentType.TEXT,
                                "content": "包含多个文本"
                            },
                            {
                                "type": ContentType.TEXT,
                                "content": "和空格"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    output_content = []
    page_idx = 0
    page_size = [1000, 1200]
    
    print("\n=== 测试复杂span的列表项 ===")
    make_list_content(BlockType.LIST, para_block, output_content, page_idx, page_size)
    
    assert len(output_content) == 1, "应该生成1个列表项"
    assert "复杂列表项" in output_content[0]['text'], "应该包含'复杂列表项'"
    assert "包含多个文本" in output_content[0]['text'], "应该包含'包含多个文本'"
    assert "和空格" in output_content[0]['text'], "应该包含'和空格'"
    print("✅ 复杂span处理正确")


if __name__ == "__main__":
    # 运行所有测试
    test_make_list_content()
    test_make_list_content_with_empty_blocks()
    test_make_list_content_with_complex_spans()
    
    print("\n" + "="*50)
    print("所有测试成功完成！")
    print("="*50)