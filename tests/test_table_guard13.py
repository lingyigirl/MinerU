#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试守卫 13（同行有损子序列残片）与守卫 12 判据 (d)（短值 CJK 列字长契约）。

覆盖 特定文档类型优化规范.md 原则 1（输出不多不少）：
滕悦银行流水实测 67 处「对方单位」残片灌入「用途」短值列——残片与同行
VLM 值的关系是「有损子序列」（丢 3~8 字），而非守卫 5B/G6B1/G6B2 覆盖的
前/后缀截断（长度差 ≤2）或编辑距离 ≤1：

- 山东汇智慧营销策划有限 ← 山东汇智慧赢营销策划有限公司（丢 赢/公/司）
- 滕州市华安装工程有     ← 滕州市华塑建筑安装工程有限责（丢 塑/建/筑/限责）

回归保护：跨行重复值（多行同名公司）不受「同行」约束影响；含 2-3 字短值
（货款/转账/材料款）不误伤；数字侧的有损子序列（余额↔发生额）不在此判据。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bs4 import BeautifulSoup

from mineru.utils.custom.table_utils import (
    _build_column_profiles,
    _fill_empty_cells_from_ocr_grid,
    _is_lossy_row_variant,
    _violates_column_contract,
)

# --- 守卫 13：_is_lossy_row_variant ---


def test_lossy_row_variant_true() -> None:
    row = ['872020200533151', '2025-02-10 14:17:02', '贷',
           '山东汇智慧赢营销策划有限公司', '313452060150', '', '转账', '125,708.61', '', '100,000.00']
    assert _is_lossy_row_variant('山东汇智慧营销策划有限', row)
    assert _is_lossy_row_variant('滕州市华安装工程有', [
        '81502010142101', '2025-07-30 09:', '贷',
        '滕州市华塑建筑安装工程有限责', '313454102246', '', '材料款', '42,078,479.05', '', '2,000,000.00'])


def test_lossy_row_variant_shape_variant_not_covered() -> None:
    """已知边界：残片含形近字替换（藤≠滕）时不构成子序列，本判据不覆盖。

    实测滕悦 p14/p34/p24 等约 10 处属此类，需形近序列比对（另行设计），
    此处显式记录边界，避免被误认为已覆盖。
    """
    row = ['87203020036788', '2025-07-30 14:', '贷',
           '山东滕建投资集团兴唐工程有限', '313454100025', '', '转账', '141,478,479.05', '', '8,100,000.00']
    assert not _is_lossy_row_variant('山东藤建资团兴唐工程', row)


def test_lossy_row_variant_not_same_row() -> None:
    """同行无该源值 → 不判（跨行同名值合法）。"""
    row = ['87203020036788', '2025-07-30 14:', '贷', '其它公司', '', '', '转账', '141,478,479.05', '', '']
    assert not _is_lossy_row_variant('山东汇智慧营销策划有限', row)


def test_lossy_row_variant_short_cjk() -> None:
    """短值（货款/转账，CJK<4 或长<6）不受影响。"""
    row = ['a', 'b', '山东汇智慧赢营销策划有限公司', 'c', '', '转账', '']
    assert not _is_lossy_row_variant('货款', row)
    assert not _is_lossy_row_variant('转账', row)


def test_lossy_row_variant_numeric_excluded() -> None:
    """数字侧不在此判据：余额 14,300,000.00 ↔ 转入金额 4,300,000.00 合法。"""
    row = ['937009010020728901', '2025-07-31 18:23:13', '借', '山东天行健发展有限公司',
           '403100000004', '往来款', '往来款', '14,300,000.00', '1,000,000.00', '']
    assert not _is_lossy_row_variant('4,300,000.00', row)
    assert not _is_lossy_row_variant('40310000', row)


def test_lossy_row_variant_exact_equal() -> None:
    row = ['山东汇智慧赢营销策划有限公司']
    assert not _is_lossy_row_variant('山东汇智慧赢营销策划有限公司', row)


# --- 守卫 12 判据 (d)：_violates_column_contract ---


def test_short_cjk_column_rejects_fragment() -> None:
    vlm = [
        ['对方账号', '交易时间', '借贷标志', '对方单位', '对方行号', '用途', '摘要', '余额', '转出', '转入'],
        ['a', '2025-07-30 14:', '贷', '山东滕建投资集团兴唐工程有限', 'x', '货款', '转账', '141,478,479.05', '', ''],
        ['b', '2025-07-30 10:', '贷', '山东滕建投资集团兴唐工程有限', 'y', '货款', '转账', '144,078,479.05', '', ''],
        ['c', '2025-07-30 09:', '贷', '滕州市华塑建筑安装工程有限责', 'z', '材料款', '转账', '42,078,479.05', '', ''],
    ]
    prof = _build_column_profiles(vlm)
    # 用途列：已有 CJK 值 货款/货款/材料款（上限 3）→ 9 字残片违约
    assert _violates_column_contract('滕州市华安装工程有', 5, prof)
    # 对方单位列：已有值均为长 CJK（上限 12 > 4）→ 字长契约不启用
    assert not _violates_column_contract('滕州市华安装工程有', 3, prof)


def test_column_contract_no_profile_inert() -> None:
    """无画像列（样本 < 3）守卫零介入。"""
    assert not _violates_column_contract('任意长文本', 0, {})


# --- 端到端：残片不再灌入空列 ---


def _fill_one(fragment: str, row_cjk_len: int) -> list[str]:
    """构造一张「对方单位」长名 + 「用途」空列的表，OCR 只给残片。"""
    long_name = '山东汇智慧赢营销策划有限公司'
    html = (
        '<table><tr><td>对方单位</td><td>用途</td></tr>'
        f'<tr><td>{long_name}</td><td></td></tr>'
        f'<tr><td>{"客户名称" * row_cjk_len}</td><td></td></tr>'
        f'<tr><td>{long_name}</td><td></td></tr>'
    )
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    ocr_grid = [[fragment, ''], ['', ''], ['', '']]
    _fill_empty_cells_from_ocr_grid(soup, table, ocr_grid)
    rows = table.find_all("tr")
    return [c.get_text().strip() for c in rows[1].find_all("td")]


def test_fragment_not_injected_into_short_cjk_column() -> None:
    cells = _fill_one('山东汇智慧营销策划有限', 2)
    assert cells[1] == '', f"残片不应落入短值 CJK 列，实际 {cells[1]!r}"
