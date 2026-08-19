#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试表格后处理 `extract_column_header_prefixes` 的列名拆分逻辑。

覆盖 特定文档类型优化规范.md 原则 11（回归测试多样性）：
针对发票表头纵向排版导致 VLM 输出「数 量」「单 价」等关键词内部含空格的场景，
验证列名与值能被拆分成两列，且无空格拼接（单位吨、金额…）行为不回退。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bs4 import BeautifulSoup

from mineru.utils.custom.table_utils import (
    extract_column_header_prefixes,
    split_summary_from_data_cell,
)


def _row_texts(html):
    """从处理后 HTML 提取每个 <tr> 的单元格文本列表。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        list[list[str]]：每个 <tr> 的单元格文本列表（保留 colspan 物理单元）。
    """
    soup = BeautifulSoup(html, "html.parser")
    return [
        [c.get_text().strip() for c in tr.find_all(["td", "th"])]
        for tr in soup.find_all("tr")
    ]


def test_extract_column_header_prefixes_splits_spaced_keyword():
    """含空格表头「数 量」「单 价」应拆分成列名 + 值两列。

    复现场景：发票表头「数量」「单价」纵向排版，VLM 输出为「数 量5203」
    「单 价0.95」（关键词内部含空格，且"量/价"与数值粘连）。旧逻辑用
    startswith("数量") 无法命中，导致列名为空、值与列名粘连。
    """
    table_html = """<table>
      <tr><td>购 买 方</td><td colspan="5">名称:某公司</td><td>密 码 区</td><td colspan="4">x</td></tr>
      <tr><td colspan="2">货物或应税劳务、服务名称*劳务*1-1居民生活污水</td><td>规格型号</td><td>单位吨</td><td>数 量5203</td><td colspan="3">单 价0.95</td><td>金额4942.85</td><td>税率免税</td><td>税额***</td></tr>
      <tr><td colspan="2">价税合计(大写)</td><td colspan="9">(小写)¥4942.85</td></tr>
    </table>"""
    out = extract_column_header_prefixes(table_html)
    rows = _row_texts(out)

    # 表头行应包含「数量」「单价」列名
    header_row = next(r for r in rows if "货物或应税劳务、服务名称" in r)
    assert "数量" in header_row, f"表头行应含「数量」，实际 {header_row}"
    assert "单价" in header_row, f"表头行应含「单价」，实际 {header_row}"

    # 数据行应含独立的值 5203、0.95，且不再含「数 量5203」「单 价0.95」
    data_row = next(r for r in rows if "5203" in r)
    assert "5203" in data_row, f"数据行应含独立值 5203，实际 {data_row}"
    assert "0.95" in data_row, f"数据行应含独立值 0.95，实际 {data_row}"
    assert not any("数 量" in c for c in data_row), f"不应残留「数 量」，实际 {data_row}"
    assert not any("单 价" in c for c in data_row), f"不应残留「单 价」，实际 {data_row}"


def test_extract_column_header_prefixes_keeps_compact_keyword():
    """无空格拼接（单位吨/金额…/税率免税/税额***）仍正确拆分，行为不回退。"""
    table_html = """<table>
      <tr><td>购 买 方</td><td colspan="5">名称:某公司</td><td>密 码 区</td><td colspan="4">x</td></tr>
      <tr><td colspan="2">货物或应税劳务、服务名称*劳务*5-生产污水</td><td>规格型号</td><td>单位吨</td><td>数量30088</td><td colspan="3">单价1.4</td><td>金额42123.20</td><td>税率免税</td><td>税额***</td></tr>
      <tr><td colspan="2">价税合计(大写)</td><td colspan="9">(小写)¥42123.20</td></tr>
    </table>"""
    out = extract_column_header_prefixes(table_html)
    rows = _row_texts(out)

    header_row = next(r for r in rows if "货物或应税劳务、服务名称" in r)
    assert "单位" in header_row, f"表头行应含「单位」，实际 {header_row}"
    assert "数量" in header_row, f"表头行应含「数量」，实际 {header_row}"
    assert "单价" in header_row, f"表头行应含「单价」，实际 {header_row}"
    assert "金额" in header_row, f"表头行应含「金额」，实际 {header_row}"

    data_row = next(r for r in rows if "30088" in r)
    assert "吨" in data_row, f"数据行应含独立值「吨」，实际 {data_row}"
    assert "30088" in data_row, f"数据行应含独立值 30088，实际 {data_row}"
    assert "42123.20" in data_row, f"数据行应含独立值 42123.20，实际 {data_row}"
    # 列名不应残留粘连在数据行
    assert not any("单位吨" in c for c in data_row), f"不应残留「单位吨」，实际 {data_row}"
    assert not any("金额42123.20" in c for c in data_row), f"不应残留「金额42123.20」，实际 {data_row}"


def test_split_summary_from_data_cell_skips_financial_statement():
    """财务报表行标签「流动资产合计」结尾含「合计」但不应被拆分。

    复现场景：资产负债表中「流动资产合计」「负债合计」等是合法的会计科目
    行标签（以「合计」结尾）。split_summary_from_data_cell 的 endswith 匹配
    曾将其误拆为「流动资产」+「合计」两行，本测试确保非发票表格跳过该处理。
    """
    table_html = """<table>
      <tr><td>资 产</td><td>2023年12月31日</td><td>2022年12月31日</td></tr>
      <tr><td>流动资产合计</td><td>30,645,434.96</td><td>12,851,478.30</td></tr>
      <tr><td>非流动资产合计</td><td>11,400,819.85</td><td>7,310,307.28</td></tr>
    </table>"""
    out = split_summary_from_data_cell(table_html)

    # 行标签应保持完整，不得拆出「流动资产」+「合计」两行
    assert "流动资产合计" in out, f"「流动资产合计」应保持完整，实际 {out}"
    assert "非流动资产合计" in out, f"「非流动资产合计」应保持完整，实际 {out}"
    assert "<td>合计</td>" not in out, f"不应拆出独立的「合计」行，实际 {out}"


def test_split_summary_from_data_cell_splits_invoice():
    """发票表格中「*供电*电费 合计」仍应拆分出「合计」行，行为不回退。

    防止加入发票门控后误伤真正的发票场景：发票表头含「项目名称/数量/单价/
    金额/税额」等关键词，可被 _is_invoice_table 识别，合计标签应继续拆分。
    """
    table_html = """<table>
      <tr><th>项目名称</th><th>规格型号</th><th>单位</th><th>数量</th><th>单价</th><th>金额</th><th>税率/征收率</th><th>税额</th></tr>
      <tr><td>*供电*电费 合计</td><td></td><td></td><td></td><td></td><td>¥4942.85</td><td></td><td>¥28973.82</td></tr>
    </table>"""
    out = split_summary_from_data_cell(table_html)

    # 「合计」应从数据标签中拆出为独立行
    assert "合计" in out, f"发票合计标签应保留，实际 {out}"
    assert "*供电*电费" in out, f"数据标签「*供电*电费」应保留，实际 {out}"


if __name__ == "__main__":
    test_extract_column_header_prefixes_splits_spaced_keyword()
    test_extract_column_header_prefixes_keeps_compact_keyword()
    test_split_summary_from_data_cell_skips_financial_statement()
    test_split_summary_from_data_cell_splits_invoice()
    print("✅ table_utils 列名拆分 + 合计拆分回归测试通过")
