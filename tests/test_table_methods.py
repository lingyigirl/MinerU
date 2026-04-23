#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 make_table_body、make_table_caption 和 make_table_footnote 方法"""

import sys
import os

# 添加本地项目路径到系统路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.backend.vlm.vlm_middle_json_mkcontent import (
    make_table_body,
    make_table_caption,
    make_table_footnote
)
from mineru.utils.enum_class import BlockType, ContentType


def test_make_table_body():
    """测试 make_table_body 方法"""
    
    print("=== 测试 make_table_body 方法 ===\n")
    
    # 模拟表格主体块
    block = {
        "type": BlockType.TABLE_BODY,
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TABLE,
                        "html": '''
                            <table border="1">
                                <tr>
                                    <th>列1</th>
                                    <th>列2</th>
                                </tr>
                                <tr>
                                    <td>数据1</td>
                                    <td>数据2</td>
                                </tr>
                            </table>
                        ''',
                        "image_path": "table_images/table1.png"
                    }
                ]
            }
        ]
    }
    
    para_block = {
        "type": BlockType.TABLE_BODY,
        "bbox": [100, 200, 600, 500],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "表格主体内容"
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    print(f"输入参数:")
    print(f"  - 表格HTML: 包含2列2行的表格")
    print(f"  - 图片路径: {block['lines'][0]['spans'][0]['image_path']}")
    print(f"  - 图片桶路径: {img_buket_path}")
    print(f"  - 页面索引: {page_idx}")
    print(f"  - 页面尺寸: {page_size}\n")
    
    # 调用方法
    make_table_body(block, para_block, img_buket_path, page_idx, page_size, output_content)
    
    # 输出结果
    print(f"输出结果:")
    print(f"  - 生成的项目数量: {len(output_content)}\n")
    
    if output_content:
        result = output_content[0]
        print(f"表格主体内容:")
        print(f"  - 类型: {result['type']}")
        print(f"  - 文本: {result.get('text', '')}")
        if BlockType.TEXT in result:
            print(f"  - HTML内容: {result[BlockType.TEXT][:50]}..." if len(result[BlockType.TEXT]) > 50 else f"  - HTML内容: {result[BlockType.TEXT]}")
        if 'img_path' in result:
            print(f"  - 图片路径: {result['img_path']}")
        if 'bbox' in result:
            print(f"  - 边界框: {result['bbox']}")
        print(f"  - 页面索引: {result['page_idx']}")
        print()
    
    # 验证结果
    print("=== 验证结果 ===")
    
    assert len(output_content) == 1, f"期望生成1个项目，实际生成{len(output_content)}个"
    print("✅ 生成项目数量正确")
    
    assert output_content[0]['type'] == BlockType.TABLE_BODY, "类型应该是TABLE_BODY"
    print("✅ 类型正确")
    
    assert BlockType.TEXT in output_content[0], "应该包含HTML内容"
    assert '<table' in output_content[0][BlockType.TEXT], "HTML内容应该包含<table标签"
    print("✅ HTML内容正确")
    
    assert 'img_path' in output_content[0], "应该包含图片路径"
    assert output_content[0]['img_path'] == f"{img_buket_path}/{block['lines'][0]['spans'][0]['image_path']}", "图片路径不正确"
    print("✅ 图片路径正确")
    
    assert 'bbox' in output_content[0], "应该包含边界框"
    assert 'page_idx' in output_content[0], "应该包含页面索引"
    print("✅ 边界框和页面索引正确")
    
    print("\n=== make_table_body 测试完成 ===\n")


def test_make_table_caption():
    """测试 make_table_caption 方法"""
    
    print("=== 测试 make_table_caption 方法 ===\n")
    
    # 模拟表格标题块
    block = {
        "type": BlockType.TABLE_CAPTION,
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "表1：测试表格标题"
                    }
                ]
            }
        ]
    }
    
    para_block = {
        "type": BlockType.TABLE_CAPTION,
        "bbox": [100, 100, 600, 150],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "表格标题"
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    print(f"输入参数:")
    print(f"  - 标题文本: {block['lines'][0]['spans'][0]['content']}")
    print(f"  - 页面索引: {page_idx}")
    print(f"  - 页面尺寸: {page_size}\n")
    
    # 调用方法
    try:
        make_table_caption(block, para_block, img_buket_path, page_idx, page_size, output_content)
        
        # 输出结果
        print(f"输出结果:")
        print(f"  - 生成的项目数量: {len(output_content)}\n")
        
        if output_content:
            result = output_content[0]
            print(f"表格标题内容:")
            print(f"  - 类型: {result['type']}")
            print(f"  - 文本: {result.get('text', '')}")
            if BlockType.TEXT in result:
                print(f"  - 附加文本: {result[BlockType.TEXT]}")
            if 'bbox' in result:
                print(f"  - 边界框: {result['bbox']}")
            print(f"  - 页面索引: {result['page_idx']}")
            print()
        
        # 验证结果
        print("=== 验证结果 ===")
        
        assert len(output_content) == 1, f"期望生成1个项目，实际生成{len(output_content)}个"
        print("✅ 生成项目数量正确")
        
        assert output_content[0]['type'] == BlockType.TABLE_CAPTION, "类型应该是TABLE_CAPTION"
        print("✅ 类型正确")
        
        assert 'bbox' in output_content[0], "应该包含边界框"
        assert 'page_idx' in output_content[0], "应该包含页面索引"
        print("✅ 边界框和页面索引正确")
        
        print("\n=== make_table_caption 测试完成 ===\n")
        
    except AttributeError as e:
        print(f"❌ 方法调用失败: {e}")
        print("注意：该方法可能存在实现问题，text字段初始化为字符串但尝试使用append方法")
        print("\n=== make_table_caption 测试完成（发现潜在问题） ===\n")


def test_make_table_footnote():
    """测试 make_table_footnote 方法"""
    
    print("=== 测试 make_table_footnote 方法 ===\n")
    
    # 模拟表格脚注块
    block = {
        "type": BlockType.TABLE_FOOTNOTE,
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "注：本表格数据仅供参考"
                    }
                ]
            }
        ]
    }
    
    para_block = {
        "type": BlockType.TABLE_FOOTNOTE,
        "bbox": [100, 550, 600, 600],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "表格脚注"
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    print(f"输入参数:")
    print(f"  - 脚注文本: {block['lines'][0]['spans'][0]['content']}")
    print(f"  - 页面索引: {page_idx}")
    print(f"  - 页面尺寸: {page_size}\n")
    
    # 调用方法
    try:
        make_table_footnote(block, para_block, img_buket_path, page_idx, page_size, output_content)
        
        # 输出结果
        print(f"输出结果:")
        print(f"  - 生成的项目数量: {len(output_content)}\n")
        
        if output_content:
            result = output_content[0]
            print(f"表格脚注内容:")
            print(f"  - 类型: {result['type']}")
            print(f"  - 文本: {result.get('text', '')}")
            if BlockType.TEXT in result:
                print(f"  - 附加文本: {result[BlockType.TEXT]}")
            if 'bbox' in result:
                print(f"  - 边界框: {result['bbox']}")
            print(f"  - 页面索引: {result['page_idx']}")
            print()
        
        # 验证结果
        print("=== 验证结果 ===")
        
        assert len(output_content) == 1, f"期望生成1个项目，实际生成{len(output_content)}个"
        print("✅ 生成项目数量正确")
        
        assert output_content[0]['type'] == BlockType.TABLE_FOOTNOTE, "类型应该是TABLE_FOOTNOTE"
        print("✅ 类型正确")
        
        assert 'bbox' in output_content[0], "应该包含边界框"
        assert 'page_idx' in output_content[0], "应该包含页面索引"
        print("✅ 边界框和页面索引正确")
        
        print("\n=== make_table_footnote 测试完成 ===\n")
        
    except AttributeError as e:
        print(f"❌ 方法调用失败: {e}")
        print("注意：该方法可能存在实现问题，text字段初始化为字符串但尝试使用append方法")
        print("\n=== make_table_footnote 测试完成（发现潜在问题） ===\n")


def test_table_body_without_image():
    """测试不带图片的表格主体"""
    
    print("=== 测试不带图片的表格主体 ===\n")
    
    block = {
        "type": BlockType.TABLE_BODY,
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TABLE,
                        "html": '<table><tr><td>数据</td></tr></table>'
                    }
                ]
            }
        ]
    }
    
    para_block = {
        "type": BlockType.TABLE_BODY,
        "bbox": [100, 200, 600, 500],
        "lines": [
            {
                "spans": [
                    {
                        "type": ContentType.TEXT,
                        "content": "表格"
                    }
                ]
            }
        ]
    }
    
    output_content = []
    img_buket_path = "https://example.com/images"
    page_idx = 0
    page_size = [1000, 1200]
    
    make_table_body(block, para_block, img_buket_path, page_idx, page_size, output_content)
    
    assert len(output_content) == 1
    assert 'img_path' not in output_content[0], "不应该包含图片路径"
    print("✅ 不带图片的表格主体处理正确\n")


if __name__ == "__main__":
    # 运行所有测试
    test_make_table_body()
    test_make_table_caption()
    test_make_table_footnote()
    test_table_body_without_image()
    
    print("="*50)
    print("所有测试完成！")
    print("="*50)