#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试金额千分位逗号被读成点号的确定性回写（amount_sep_repair）。

覆盖 特定文档类型优化规范.md 原则 1（输出不多不少）：
新发银行流水实测 VLM 原生把千分位 `,` 读成 `.`——96 页中 26 个 token 落在
2 页（p19 25 处 / p84 1 处），全部位于表格 <td> 内。

- p19「余额」列 20/20 全为点号形态（3.991.623.04 …）
- p19「借方/贷方发生额」列为混合形态（18 逗号 + 2 点号 / 17 逗号 + 3 点号）
- p84「贷方发生额」19 逗号 + 1 点号（6.407.71，致余额链断裂额恰为该值）

回归保护：日期（2025.03.07）、无尾段歧义形态（1.234.567）、非法分组
（1.2345.67）、长串内嵌（NO.138.001.23）、非金额列内的点号串一律不改写。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.utils.custom.table_utils.amount_sep_repair import (
    _amount_columns,
    _has_money_context,
    _transcode_dotted_amount,
    repair_dotted_amount_separators,
)

# --- 转码：点号形态 → 逗号形态 ---


def test_transcode_dotted_amount_positive() -> None:
    # p19 余额列（三段式）
    assert _transcode_dotted_amount('3.991.623.04') == '3,991,623.04'
    assert _transcode_dotted_amount('3.991.873.28') == '3,991,873.28'
    assert _transcode_dotted_amount('3.833.882.90') == '3,833,882.90'
    # p19 借贷方（两段式）
    assert _transcode_dotted_amount('6.864.34') == '6,864.34'
    assert _transcode_dotted_amount('100.000.00') == '100,000.00'
    assert _transcode_dotted_amount('70.000.00') == '70,000.00'
    assert _transcode_dotted_amount('2.046.45') == '2,046.45'
    assert _transcode_dotted_amount('4.341.35') == '4,341.35'
    # p84 贷方（单处）
    assert _transcode_dotted_amount('6.407.71') == '6,407.71'
    # 更多段
    assert _transcode_dotted_amount('1.234.567.89') == '1,234,567.89'
    # 前后空白保留
    assert _transcode_dotted_amount('  3.991.623.04  ') == '3,991,623.04'


def test_transcode_keeps_prefix() -> None:
    """货币符号 / 正负号原样保留，仅分隔符改写。"""
    assert _transcode_dotted_amount('¥3.991.623.04') == '¥3,991,623.04'
    assert _transcode_dotted_amount('￥3.991.623.04') == '¥3,991,623.04'
    assert _transcode_dotted_amount('-6.864.34') == '-6,864.34'
    assert _transcode_dotted_amount('+1.234.56') == '+1,234.56'


def test_transcode_rejects_ambiguous_and_foreign_shapes() -> None:
    """反例：一律返回 None（不改写）。"""
    # 日期：头段 4 位，本不需千分位
    assert _transcode_dotted_amount('2025.03.07') is None
    # 无 2 位尾段的歧义形态（版本号/编号类）；全文档 0 处
    assert _transcode_dotted_amount('1.234.567') is None
    # 中段非 3 位
    assert _transcode_dotted_amount('1.2345.67') is None
    # 长串内嵌：整格不匹配
    assert _transcode_dotted_amount('NO.138.001.23') is None
    assert _transcode_dotted_amount('凭证 3.991.623.04 号') is None
    assert _transcode_dotted_amount('3.991.623.04元') is None
    # 已是正确形态：无需改写
    assert _transcode_dotted_amount('3,991,623.04') is None
    assert _transcode_dotted_amount('100,000.00') is None
    # 无小数点的裸整数 / 单点小数
    assert _transcode_dotted_amount('3991623') is None
    assert _transcode_dotted_amount('3991623.04') is None
    # 非金额
    assert _transcode_dotted_amount('') is None
    assert _transcode_dotted_amount('abc') is None


# --- 表级闸门：金额上下文 ---


def test_has_money_context_by_header_keyword() -> None:
    """表头含金额关键词即视为金额表（流水表的 余额/借方/贷方 均在此列）。"""
    assert _has_money_context(['交易时间', '贷方发生额', '余额'], [])
    assert _has_money_context(['摘要', '金额'], [])


def test_has_money_context_by_unambiguous_amount_column() -> None:
    """表头无关键词时，某列以含逗号/纯小数形态为主亦可证明是金额表。"""
    rows = [[v] for v in ['5,179.30', '1,415.74', '1,000.00']]
    assert _has_money_context(['往来', '其它'], rows)


def test_has_money_context_false_for_non_money_table() -> None:
    """无关键词、也无无歧义金额列 → 无金额证据，整表不介入。"""
    assert not _has_money_context(
        ['联系方式', '备注'],
        [['138.001.23', 'A001'], ['139.002.34', 'B002'], ['137.003.45', 'C003']],
    )


# --- 列级金额投票 ---


def test_amount_columns_full_dotted_column() -> None:
    """p19 余额列形态：20/20 全点号。include_dotted=True 时必须计入命中，
    否则该列 amount_ratio = 0、被判非金额列，20 处修复全部落空。"""
    rows = [['3.991.623.04'], ['3.991.873.28'], ['3.992.171.48'], ['3.892.171.48']]
    assert _amount_columns(rows, include_dotted=True) == {0}
    # 同一列在表级闸门口径下不算「无歧义金额列」（点号形态有歧义）
    assert _amount_columns(rows, include_dotted=False) == set()


def test_amount_columns_mixed_column() -> None:
    """p19 贷方形态：17 逗号 + 3 点号 → 金额列判定通过。"""
    rows = [[v] for v in (
        ['5,179.30', '1,415.74', '3,384.57', '1,014.40', '1,800.28']
        + ['6.864.34', '2.046.45', '4.341.35']
        + ['1,000.00'] * 9
    )]
    assert _amount_columns(rows, include_dotted=True) == {0}


def test_amount_columns_single_outlier() -> None:
    """p84 贷方形态：19 逗号 + 1 点号 → 通过，该孤立点号得以修复。"""
    rows = [[v] for v in (['1,000.00'] * 19 + ['6.407.71'])]
    assert _amount_columns(rows, include_dotted=True) == {0}


def test_amount_columns_rejects_non_amount_column() -> None:
    """非金额列不改写：文本列不获授权。"""
    text_col = [[v] for v in ['深圳市神州路通技术有限公司', '山东汇智慧赢营销策划有限公司', '滕州市华塑建筑安装工程']]
    assert _amount_columns(text_col, include_dotted=True) == set()


def test_amount_columns_sample_floor() -> None:
    """样本数 < _MIN_PROFILE_SAMPLES(3) 的列不介入（保守优先）。"""
    assert _amount_columns([['3.991.623.04'], ['3.991.873.28']], include_dotted=True) == set()


# --- 入口：就地改写 PDF info 的 span html ---


def _page_with_table(html: str) -> dict:
    """构造一条最小 pdf_info：表 span 嵌在 blocks -> lines -> spans 内。"""
    return {
        "preproc_blocks": [{
            "type": "table",
            "blocks": [{
                "type": "table_body",
                "lines": [{"spans": [{"type": "table", "html": html}]}],
            }],
        }]
    }


def _cell_texts(html: str) -> list[list[str]]:
    import re
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)
    return [
        [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.S)]
        for r in rows
    ]


def test_repair_rewrites_full_dotted_amount_column() -> None:
    """p19 余额列：整列 20 行点号全部回写为逗号。"""
    body = ''.join(f'<tr><td>2025-03-07</td><td>{v}</td></tr>' for v in (
        '3.991.623.04', '3.991.873.28', '3.992.171.48', '3.892.171.48', '3.886.992.18',
    ))
    html = f'<table><tr><td>交易时间</td><td>余额</td></tr>{body}</table>'
    page = _page_with_table(html)

    fixed = repair_dotted_amount_separators([page])

    assert fixed == 5
    out = page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html']
    assert '3.991.623.04' not in out
    assert '3,991,623.04' in out
    assert _cell_texts(out)[1] == ['2025-03-07', '3,991,623.04']


def test_repair_only_touches_dotted_cells_in_mixed_column() -> None:
    """p19 贷方形态：混合列内仅点号被改写，逗号值逐位不变。"""
    values = ['5,179.30', '1,415.74', '1,000.00', '6.864.34', '2,000.00']
    body = ''.join(f'<tr><td>摘要</td><td>{v}</td></tr>' for v in values)
    html = f'<table><tr><td>摘要</td><td>贷方发生额</td></tr>{body}</table>'
    page = _page_with_table(html)

    fixed = repair_dotted_amount_separators([page])

    assert fixed == 1
    out = page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html']
    assert [r[1] for r in _cell_texts(out)[1:]] == [
        '5,179.30', '1,415.74', '1,000.00', '6,864.34', '2,000.00',
    ]


def test_repair_leaves_non_amount_column_untouched() -> None:
    """无金额证据的表整体不介入：整列点号的电话号列表得救。"""
    html = (
        '<table><tr><td>联系方式</td><td>备注</td></tr>'
        '<tr><td>138.001.23</td><td>A001</td></tr>'
        '<tr><td>139.002.34</td><td>B002</td></tr>'
        '<tr><td>137.003.45</td><td>C003</td></tr></table>'
    )
    page = _page_with_table(html)

    assert repair_dotted_amount_separators([page]) == 0
    assert '138.001.23' in page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html']


def test_repair_leaves_minority_dotted_column_in_amount_table() -> None:
    """金额表内，点号只占少数的非金额列不获授权（列投票拦住）。"""
    html = (
        '<table><tr><td>金额</td><td>联系方式</td></tr>'
        '<tr><td>1,000.00</td><td>138.001.23</td></tr>'
        '<tr><td>2,000.00</td><td>A001</td></tr>'
        '<tr><td>3,000.00</td><td>B002</td></tr>'
        '<tr><td>4,000.00</td><td>C003</td></tr></table>'
    )
    page = _page_with_table(html)

    assert repair_dotted_amount_separators([page]) == 0
    out = page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html']
    assert _cell_texts(out)[1][1] == '138.001.23'


def test_repair_is_idempotent() -> None:
    """幂等：二次调用零改写（已修复形态不再匹配）。"""
    body = ''.join(f'<tr><td>{v}</td></tr>' for v in ('3.991.623.04', '3.991.873.28', '3.992.171.48'))
    html = f'<table><tr><td>余额</td></tr>{body}</table>'
    page = _page_with_table(html)

    assert repair_dotted_amount_separators([page]) == 3
    before = page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html']
    assert repair_dotted_amount_separators([page]) == 0
    assert page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html'] == before


def test_repair_skips_header_row() -> None:
    """表头行不改写——即使该格本身是点号形态，且所在列已获授权。"""
    body = ''.join(f'<tr><td>{a}</td><td>{b}</td></tr>' for a, b in (
        ('1.000.00', '2.000.00'), ('3.000.00', '4.000.00'), ('5.000.00', '6.000.00'),
    ))
    html = f'<table><tr><td>金额</td><td>1.234.56</td></tr>{body}</table>'
    page = _page_with_table(html)

    assert repair_dotted_amount_separators([page]) == 6

    out = page['preproc_blocks'][0]['blocks'][0]['lines'][0]['spans'][0]['html']
    assert _cell_texts(out)[0] == ['金额', '1.234.56']  # 表头原样
    assert _cell_texts(out)[1] == ['1,000.00', '2,000.00']


def test_repair_ignores_non_table_spans_and_missing_html() -> None:
    """非表 span、空 html、无 <table> 均安全跳过。"""
    pages = [
        {"preproc_blocks": [{"type": "text", "lines": [{"spans": [{"type": "text", "content": "3.991.623.04"}]}]}]},
        _page_with_table(''),
        _page_with_table('<div>3.991.623.04</div>'),
        _page_with_table('<table><tr><td>余额</td></tr></table>'),
    ]
    assert repair_dotted_amount_separators(pages) == 0
