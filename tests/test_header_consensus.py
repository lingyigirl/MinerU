#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试守卫 11 扩展版（跨页表头一致性归一化）。

背景：滕悦银行流水 42 页表格中 32 页第 9 列表头被印章叠印污染为
"专用章转出金额"（30 页）/"专用转出金额"（2 页），正确表头是 "转出金额"
（10 页干净）。原生守卫 11 只用 `text.startswith(base)` 判后缀污染，
方向反了；且它挂在 hybrid 的 OCR 补充钩子内，VLM 路由整组跳过。

覆盖：
- `_is_cjk_only` / `_affix_form`：双向前后缀识别与字符合法性
- `_pick_column_base`：被另一张表污染的同列、单页截断异常值、
  共识不足
- `normalize_table_headers_across_pages`：前缀/后缀两个方向的改写、
  同文档另一张表的合法表头不受牵连、"金额(元)" 不被裁、
  印章词表闸门
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bs4 import BeautifulSoup

from mineru.utils.custom.table_utils.header_consensus import (
    _affix_form,
    _is_cjk_only,
    _pick_column_base,
    normalize_table_headers_across_pages,
)

# --- 字符与前后缀判据 ---

CANON = ['对方账号', '交易时间', '借贷标志', '对方单位', '对方行号',
         '用途', '摘要', '余额', '转出金额', '转入金额']


def _html(cells: list[str]) -> str:
    return '<table><tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr></table>'


def _page(cells: list[str], page_idx: int = 0) -> dict:
    """构造一页含单张表格的中间 JSON 页面（结构对齐 VLM/hybrid 真实产出：
    table 块 -> table_body 子块 -> lines[].spans[]，span 带 html）。"""
    return {
        "page_idx": page_idx,
        "preproc_blocks": [
            {
                "type": "table",
                "bbox": [0, 0, 100, 100],
                "index": 0,
                "blocks": [
                    {
                        "type": "table_body",
                        "bbox": [0, 0, 100, 100],
                        "index": 0,
                        "lines": [{"spans": [{"type": "table", "html": _html(cells)}]}],
                    }
                ],
            }
        ],
    }


def _seal_page(text: str, page_idx: int = 99) -> dict:
    """构造一页只含印章 image 块的页面，供 _collect_doc_seals 取词表。"""
    return {
        "page_idx": page_idx,
        "preproc_blocks": [
            {
                "type": "image",
                "bbox": [0, 0, 100, 100],
                "index": 0,
                "lines": [{"spans": [{"type": "image", "content": text}]}],
            }
        ],
    }


def _headers(pdf_info: list) -> list[list[str]]:
    """取回每页首行表头文本。"""
    out = []
    for page in pdf_info:
        for block in page.get("preproc_blocks", []):
            for sub in block.get("blocks", []):
                for line in sub.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") != "table" or not span.get("html"):
                            continue
                        table = BeautifulSoup(span["html"], "html.parser").find("table")
                        if table is None:
                            continue
                        row = table.find("tr")
                        if row is None:
                            continue
                        out.append([c.get_text().strip() for c in row.find_all(["td", "th"])])
    return out


def _replace_last(cells: list[str], value: str) -> list[str]:
    return cells[:-2] + [value, cells[-1]]


# --- _is_cjk_only ---


def test_is_cjk_only() -> None:
    assert _is_cjk_only('专用章')
    assert not _is_cjk_only('(元)')
    assert not _is_cjk_only('12')
    assert not _is_cjk_only('abc')
    assert not _is_cjk_only('')


# --- _affix_form ---


def test_affix_form_prefix_direction() -> None:
    """滕悦场景：剥离部分在头部。"""
    stripped, is_prefix = _affix_form('专用章转出金额', '转出金额')
    assert stripped == '专用章' and is_prefix


def test_affix_form_suffix_direction() -> None:
    """原生守卫 11 场景：剥离部分在尾部。"""
    stripped, is_prefix = _affix_form('转出金额专用章', '转出金额')
    assert stripped == '专用章' and not is_prefix


def test_affix_form_unrelated_and_overlong() -> None:
    """无关文本与超长后缀都不算脏形式。"""
    assert _affix_form('转入金额', '转出金额')[0] is None
    assert _affix_form('金额(元)', '金额')[0] == '(元)'
    # 长度差超过 4 视为无关（印章叠印文字不会那么长）
    assert _affix_form('转出金额业务专用章编号', '转出金额')[0] is None
    # 基准本身（长度差 0）不是脏形式
    assert _affix_form('转出金额', '转出金额')[0] is None


# --- _pick_column_base ---


def test_base_survives_other_table_pollution() -> None:
    """同列索引被另一张列结构不同的表污染时仍能选出真基准。

    滕悦实测：6 页另一格式流水表把「余额」放在第 9 列，若取最短文本，
    基准会变成「余额」，整个守卫空转。
    """
    texts = ['专用章转出金额'] * 30 + ['转出金额'] * 10 + ['余额'] * 6 + ['专用转出金额'] * 2
    assert _pick_column_base(texts) == '转出金额'


def test_base_ignores_single_page_truncation() -> None:
    """单页截断形状下不得误伤合法表头。

    基线选择对「10 页转出金额 + 1 页转出」这种形状无法区分它是单页截断
    还是「少量干净页 + 多数污染页」（都是多数长、少数短），故安全性由
    印章词表闸门保证：无印章证据 → 零改写。
    """
    pdf_info = [_page(_replace_last(CANON, '转出金额'), i) for i in range(9)]
    pdf_info.append(_page(_replace_last(CANON, '转出'), 9))
    assert normalize_table_headers_across_pages(pdf_info) == 0
    for headers in _headers(pdf_info):
        assert headers[8] in ('转出金额', '转出')
    assert sum(1 for h in _headers(pdf_info) if h[8] == '转出金额') == 9


def test_base_none_when_consensus_insufficient() -> None:
    """共识不足（每个变体只出现一次）时整列跳过。"""
    assert _pick_column_base(['转出金额']) is None
    assert _pick_column_base(['转出金额', '转入金额']) is None


# --- 端到端 ---


def test_prefix_contamination_is_fixed() -> None:
    """滕悦三变体归一：32 处前缀污染改写为规范表头。"""
    pdf_info = [
        _page(CANON, 0),
        _page(_replace_last(CANON, '转出金额'), 1),
        _page(_replace_last(CANON, '专用章转出金额'), 2),
        _page(_replace_last(CANON, '专用章转出金额'), 3),
        _page(_replace_last(CANON, '专用转出金额'), 4),
        _seal_page('中国工商银行股份有限公司\n枣庄三八支行\n业务专用章\n编号123'),
    ]
    fixes = normalize_table_headers_across_pages(pdf_info)

    assert fixes == 3, f"应改写 3 处，实际 {fixes}"
    for headers in _headers(pdf_info):
        assert headers[8] == '转出金额'


def test_suffix_contamination_still_fixed() -> None:
    """原生守卫 11 的尾部方向不能被这次扩展破坏。"""
    pdf_info = [
        _page(CANON, 0),
        _page(_replace_last(CANON, '转出金额专用章'), 1),
        _page(_replace_last(CANON, '转出金额专用章'), 2),
        _seal_page('业务专用章'),
    ]
    fixes = normalize_table_headers_across_pages(pdf_info)

    assert fixes == 2
    for headers in _headers(pdf_info):
        assert headers[8] == '转出金额'


def test_identical_pages_untouched() -> None:
    """所有页表头一致时零改写（幂等，可重复调用）。"""
    pdf_info = [_page(CANON, i) for i in range(5)]
    assert normalize_table_headers_across_pages(pdf_info) == 0
    assert normalize_table_headers_across_pages(pdf_info) == 0


def test_unit_suffix_in_header_not_stripped() -> None:
    """"金额(元)" 不得被裁成 "金额"（剥离部分非纯 CJK）。"""
    cells = ['项目', '金额(元)']
    pdf_info = [
        _page(cells, 0),
        _page(cells, 1),
        _page(['项目', '金额(元)'], 2),
        _seal_page('业务专用章'),
    ]
    assert normalize_table_headers_across_pages(pdf_info) == 0
    for headers in _headers(pdf_info):
        assert headers[1] == '金额(元)'


def test_seal_vocabulary_gate_blocks_unrelated_strip() -> None:
    """文档有印章词表时，剥离部分必须落在词表内——否则保守跳过。

    这是防误伤「同列不同表头」的闸门：
    "对方单位名称" 与 "对方单位" 分属两张表，仅靠字长差无法区分。
    """
    cells = ['序号', '对方单位']
    other = ['序号', '对方单位名称']
    pdf_info = [
        _page(cells, 0),
        _page(cells, 1),
        _page(other, 2),
        _page(other, 3),
        _seal_page('业务专用章'),
    ]
    # "名称" 不在印章词表内 -> 不改写；基准仍选出 "对方单位" 但零替换
    assert normalize_table_headers_across_pages(pdf_info) == 0
    names = [h[1] for h in _headers(pdf_info)]
    assert '对方单位名称' in names


def test_no_seal_evidence_blocks_all_rewrites() -> None:
    """文档无印章证据时一律不改写（防误伤合法表头的强闸门）。"""
    pdf_info = [
        _page(_replace_last(CANON, '转出金额'), 0),
        _page(_replace_last(CANON, '专用章转出金额'), 1),
        _page(_replace_last(CANON, '专用章转出金额'), 2),
    ]
    assert normalize_table_headers_across_pages(pdf_info) == 0
    names = [h[8] for h in _headers(pdf_info)]
    assert names.count('专用章转出金额') == 2


def test_single_clean_page_plus_contamination_fixed() -> None:
    """仅 1 页干净 + 2 页污染时仍能归一（基线取覆盖度最高者，非众数）。"""
    pdf_info = [
        _page(_replace_last(CANON, '转出金额'), 0),
        _page(_replace_last(CANON, '转出金额专用章'), 1),
        _page(_replace_last(CANON, '转出金额专用章'), 2),
        _seal_page('业务专用章'),
    ]
    fixes = normalize_table_headers_across_pages(pdf_info)
    assert fixes == 2
    for headers in _headers(pdf_info):
        assert headers[8] == '转出金额'


def test_other_table_headers_preserved() -> None:
    """同文档另一张列结构不同的表（第 9 列是「余额」）不得被牵连。"""
    pdf_info = [
        _page(CANON, i) for i in range(4)
    ] + [
        _page(['日期', '业务产品种类', '凭证种类', '凭证号', '对方户名',
               '摘要', '借方发生额', '贷方发生额', '余额', '记账信息'], 10 + i)
        for i in range(3)
    ] + [
        _page(_replace_last(CANON, '专用章转出金额'), 20),
        _page(_replace_last(CANON, '专用章转出金额'), 21),
        _seal_page('业务专用章'),
    ]
    fixes = normalize_table_headers_across_pages(pdf_info)

    assert fixes == 2
    headers = _headers(pdf_info)
    # 主表全部归一
    for h in headers:
        if h[0] == '对方账号':
            assert h[8] == '转出金额'
    # 另一张表原样保留
    other = [h for h in headers if h[0] == '日期']
    assert len(other) == 3
    for h in other:
        assert h[8] == '余额' and h[9] == '记账信息'
