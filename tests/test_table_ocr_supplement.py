#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试表格 OCR 补充中 data_row_start 检测逻辑（回归测试）。

覆盖 特定文档类型优化规范.md 原则 11（回归测试多样性）：
针对「多行表头 + 大量数值列为空的标签数据行」的所有者权益变动表场景，
验证 _detect_data_row_start 不会把 data_row_start 一路推进到表格末尾。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bs4 import BeautifulSoup

from mineru.utils.custom.table_utils import (
    _detect_data_row_start,
    _parse_vlm_table_structure,
    _is_structurally_sparse_table,
    _rebuild_merged_rows_from_ocr,
)


def _rows_from_html(table_html):
    """从 HTML 中提取 <tr> 标签列表。

    Args:
        table_html: 表格 HTML 字符串。

    Returns:
        (rows, vlm_data): <tr> 标签列表和展开后的文本网格。
    """
    soup = BeautifulSoup(table_html, "html.parser")
    rows = soup.find_all("tr")
    vlm_data, _ = _parse_vlm_table_structure(rows)
    return rows, vlm_data


def test_equity_statement_label_only_rows_not_treated_as_header():
    """所有者权益变动表：数值列为空的标签数据行不得被误判为表头。

    复现场景：表头后有多个「标签 + 空数值」的数据行（如"加:会计政策变更"），
    其中穿插了含数值的数据行。旧逻辑会在遇到空值标签行时继续推进
    data_row_start，最终落到表格末尾行，导致 OCR 表头文字被错误填充进末行。
    """
    table_html = """
    <table>
      <tr><th>项目</th><th>2022年度</th><th>2023年度</th></tr>
      <tr><td>一、上期期末余额</td><td>120,000.00</td><td>130,000.00</td></tr>
      <tr><td>加:会计政策变更</td><td></td><td></td></tr>
      <tr><td>二、本期期初余额</td><td>120,000.00</td><td>130,000.00</td></tr>
      <tr><td>1.提取盈余公积</td><td></td><td></td></tr>
      <tr><td>四、本期期末余额</td><td></td><td></td></tr>
    </table>
    """
    rows, vlm_data = _rows_from_html(table_html)
    data_row_start = _detect_data_row_start(rows, vlm_data)

    # 表头只有第 0 行（<th>），第 1 行是首个数据行（含数值）。
    # 旧逻辑会返回 5（末行"四、本期期末余额"），修复后应返回 1。
    assert data_row_start == 1, (
        f"data_row_start 应为首个数据行索引 1，实际 {data_row_start}"
    )


def test_simple_header_and_data():
    """简单表格：单行表头 + 数据行。"""
    table_html = """
    <table>
      <tr><th>名称</th><th>金额</th></tr>
      <tr><td>营业收入</td><td>1,000.00</td></tr>
      <tr><td>营业成本</td><td>600.00</td></tr>
    </table>
    """
    rows, vlm_data = _rows_from_html(table_html)
    data_row_start = _detect_data_row_start(rows, vlm_data)
    assert data_row_start == 1, (
        f"data_row_start 应为 1，实际 {data_row_start}"
    )


def test_no_header_first_row_is_data():
    """无表头：首行即数据行（含数值）。"""
    table_html = """
    <table>
      <tr><td>营业收入</td><td>1,000.00</td></tr>
      <tr><td>营业成本</td><td>600.00</td></tr>
    </table>
    """
    rows, vlm_data = _rows_from_html(table_html)
    data_row_start = _detect_data_row_start(rows, vlm_data)
    assert data_row_start == 0, (
        f"data_row_start 应为 0，实际 {data_row_start}"
    )


def test_image_row_terminates_header():
    """含 <img> 的数据行应终止表头扩展（含签章图片的行是数据行）。"""
    table_html = """
    <table>
      <tr><th>项目</th><th>金额</th></tr>
      <tr><td>签章</td><td><img src="x.png"/></td></tr>
      <tr><td>下一行</td><td></td></tr>
    </table>
    """
    rows, vlm_data = _rows_from_html(table_html)
    data_row_start = _detect_data_row_start(rows, vlm_data)
    assert data_row_start == 1, (
        f"data_row_start 应为 1，实际 {data_row_start}"
    )


def _table_from_html(table_html):
    """从 HTML 中提取 <table> Tag。

    Args:
        table_html: 表格 HTML 字符串。

    Returns:
        BeautifulSoup <table> Tag。
    """
    soup = BeautifulSoup(table_html, "html.parser")
    return soup.find("table")


def test_is_structurally_sparse_table():
    """结构性稀疏表格（空 td 占比 > 30%）应判定为稀疏。

    覆盖财务报表/征信报告等"矩阵稀疏、空单元格为合法留白"的场景，
    验证门控会跳过这类表格的 OCR 补充，避免表头文字/行标签误填。
    """
    # 稀疏表：15 个 td 中 6 个为空（40%）
    sparse_html = """
    <table>
      <tr><td>项目</td><td>2022年度</td><td>2023年度</td></tr>
      <tr><td>一、上期期末余额</td><td>120,000.00</td><td>130,000.00</td></tr>
      <tr><td>加:会计政策变更</td><td></td><td></td></tr>
      <tr><td>1.提取盈余公积</td><td></td><td></td></tr>
      <tr><td>四、本期期末余额</td><td></td><td></td></tr>
    </table>
    """
    sparse_table = _table_from_html(sparse_html)
    assert _is_structurally_sparse_table(sparse_table) is True, (
        "空 td 占比 40% 应判定为稀疏"
    )

    # 稠密表：6 个 td 全有内容（0%）
    dense_html = """
    <table>
      <tr><td>项目</td><td>金额</td><td>税额</td></tr>
      <tr><td>*供电*电费</td><td>619.47</td><td>80.53</td></tr>
    </table>
    """
    dense_table = _table_from_html(dense_html)
    assert _is_structurally_sparse_table(dense_table) is False, (
        "无空 td 应判定为非稀疏"
    )

    # 边界：6 个 td 中 1 个空（约 17%）
    edge_html = """
    <table>
      <tr><td>项目</td><td>金额</td><td>税额</td></tr>
      <tr><td>*供电*电费</td><td></td><td>80.53</td></tr>
    </table>
    """
    edge_table = _table_from_html(edge_html)
    assert _is_structurally_sparse_table(edge_table) is False, (
        "空 td 占比 17% 应判定为非稀疏"
    )


def _rebuild_invoice_table(table_html, ocr_grid):
    """用 OCR 网格重建发票表格，返回重建后的 <tr> 文本网格。

    Args:
        table_html: 表格 HTML 字符串（含拼接数据行）。
        ocr_grid: OCR 识别文字网格（每行内按 x 排序）。

    Returns:
        重建后每个 <tr> 的单元格文本列表（list[list[str]]）。
    """
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    rows = table.find_all("tr")
    vlm_data, vlm_cells = _parse_vlm_table_structure(rows)
    data_row_start = _detect_data_row_start(rows, vlm_data)
    _rebuild_merged_rows_from_ocr(
        soup, table, ocr_grid, vlm_data, vlm_cells, data_row_start
    )
    return [
        [c.get_text().strip() for c in tr.find_all(["td", "th"])]
        for tr in table.find_all("tr")
    ]


# 发票模板物理列顺序（8 列）：货物/服务(colspan=2)、规格型号、单位、
# 数量、单价(colspan=2)、金额、税率、税额
_AMOUNT_COL_IDX = 5
_TAX_COL_IDX = 7


def test_ocr_rebuild_recovers_amount_summary_yen():
    """OCR 重建：VLM 金额列缺金额合计时，应从 OCR 合计行恢复金额 ¥ 值。

    复现场景：VLM 原始「金额」单元格只有两行数据值（无金额合计），
    「税额」单元格含税额合计。OCR 合计行按 x 排序为
    ["合计",...,"¥4438.85",...,"¥71.95"]。旧逻辑会因 ¥ 值仅通过文本
    子串匹配而丢弃 ¥4438.85，并把残余的 ¥71.95 错位到金额列。
    修复后合计行应金额=¥4438.85、税额=¥71.95。
    """
    table_html = """<table>
      <tr><td>购
买
方</td><td colspan="5">名称:赣州鑫冠科技股份有限公司纳税人识别号:91360700589231468Y(消防)</td><td>密
码
区</td><td colspan="3">0367</td></tr>
      <tr><td colspan="2">货物或应税劳务、服务名称*水冰雪*生活一阶*劳务*1类民用污水合计</td><td>规格型号</td><td>单位</td><td>数 量
2148
2148</td><td colspan="2">单 价
1.116504
0.95</td><td>金额
2398.25
2040.60</td><td>税率
3%
免税</td><td>税 额
71.95
***
¥71.95</td></tr>
      <tr><td colspan="2">价税合计(大写)</td><td colspan="8">×肆仟伍佰壹拾圆零捌角整 (小写)¥4510.80</td></tr>
    </table>"""
    ocr_grid = [
        ["货物或应税劳务", "服务名称", "规格型号", "单位", "数量", "单价", "金额", "税率", "税额"],
        ["*水冰雪*生活一阶", "", "", "", "2148", "1.116504", "2398.25", "3%", "71.95"],
        ["*劳务*1类民用污水", "", "", "", "2148", "0.95", "2040.60", "免税", "***"],
        ["合计", "", "", "", "", "", "¥4438.85", "", "¥71.95"],
    ]
    rebuilt = _rebuild_invoice_table(table_html, ocr_grid)
    summary_row = next(r for r in rebuilt if r and r[0] == "合计")
    assert summary_row[_AMOUNT_COL_IDX] == "¥4438.85", (
        f"金额列应为 ¥4438.85，实际 {summary_row}"
    )
    assert summary_row[_TAX_COL_IDX] == "¥71.95", (
        f"税额列应为 ¥71.95，实际 {summary_row}"
    )


def test_ocr_rebuild_summary_yen_unchanged_when_template_has_both():
    """OCR 重建：VLM 金额列与税额列均含合计时，合计行 ¥ 值保持不变。

    回归保护：流量计式发票（VLM 已识别金额合计和税额合计）修复前后应一致，
    金额=¥81387.23、税额=¥1368.85。
    """
    table_html = """<table>
      <tr><td>购
买
方</td><td colspan="5">名称:赣州鑫冠科技股份有限公司纳税人识别号:91360700589231468Y(流量计)</td><td>密
码
区</td><td colspan="3">03</td></tr>
      <tr><td colspan="2">货物或应税劳务、服务名称*水冰雪*生产*劳务*1类生产污水合计</td><td>规格型号</td><td>单位</td><td>数 量
25542
25542</td><td colspan="2">单 价
1.786408
1.4</td><td>金额
45628.43
35758.80
¥81387.23</td><td>税率
3%
免税</td><td>税 额
1368.85
***
¥1368.85</td></tr>
      <tr><td colspan="2">价税合计(大写)</td><td colspan="8">☒捌万贰仟柒佰伍拾陆圆零捌分 (小写)¥82756.08</td></tr>
    </table>"""
    ocr_grid = [
        ["货物或应税劳务", "服务名称", "规格型号", "单位", "数量", "单价", "金额", "税率", "税额"],
        ["*水冰雪*生产", "", "", "", "25542", "1.786408", "45628.43", "3%", "1368.85"],
        ["*劳务*1类生产污水", "", "", "", "25542", "1.4", "35758.80", "免税", "***"],
        ["合计", "", "", "", "", "", "¥81387.23", "", "¥1368.85"],
    ]
    rebuilt = _rebuild_invoice_table(table_html, ocr_grid)
    summary_row = next(r for r in rebuilt if r and r[0] == "合计")
    assert summary_row[_AMOUNT_COL_IDX] == "¥81387.23", (
        f"金额列应为 ¥81387.23，实际 {summary_row}"
    )
    assert summary_row[_TAX_COL_IDX] == "¥1368.85", (
        f"税额列应为 ¥1368.85，实际 {summary_row}"
    )


if __name__ == "__main__":
    test_equity_statement_label_only_rows_not_treated_as_header()
    test_simple_header_and_data()
    test_no_header_first_row_is_data()
    test_image_row_terminates_header()
    test_is_structurally_sparse_table()
    test_ocr_rebuild_recovers_amount_summary_yen()
    test_ocr_rebuild_summary_yen_unchanged_when_template_has_both()
    print("✅ 所有 _detect_data_row_start / 稀疏门控 / OCR 重建 回归测试通过")
