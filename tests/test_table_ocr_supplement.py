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
    _is_financial_statement_table,
    _rebuild_merged_rows_from_ocr,
    _fill_empty_cells_from_ocr_grid,
    _split_concatenated_row_deterministically,
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


def _fill_table_from_grid(table_html, ocr_grid):
    """用 OCR 网格填充表格，返回填充后的表格文本。

    Args:
        table_html: 表格 HTML 字符串。
        ocr_grid: OCR 识别文字网格（每行内按 x 排序）。

    Returns:
        (modified, table_text)：是否修改，以及填充后表格的纯文本。
    """
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    modified = _fill_empty_cells_from_ocr_grid(soup, table, ocr_grid)
    return modified, table.get_text()


def test_fill_empty_cells_skips_fullwidth_duplicate_labels():
    """全角分区标题与 VLM 半角标签重复时不应被复制填充（原则 1：输出不多不少）。

    资产负债表式场景：VLM 已含半角"递延税项:"（同行）与"流动资产:"/"流动负债:"
    （顶部标题行），OCR 读出全角"递延税项："/"流动资产："/"流动负债："。旧逻辑仅
    同行精确去重，全角逃过去重被填入空的行次/期末数列，产生重复字段。
    """
    table_html = """<table>
      <tr><td>资 产</td><td>行次</td><td>年初数</td><td>期末数</td><td>负债</td><td>行次</td><td>年初数</td><td>期末数</td></tr>
      <tr><td>流动资产:</td><td></td><td></td><td></td><td>流动负债:</td><td></td><td></td><td></td></tr>
      <tr><td>货币资金</td><td>1</td><td>100.00</td><td>200.00</td><td>短期借款</td><td>68</td><td>100.00</td><td></td></tr>
      <tr><td>递延税项:</td><td></td><td></td><td></td><td>未分配利润</td><td>121</td><td>300.00</td><td>400.00</td></tr>
    </table>"""
    ocr_grid = [
        ["资 产", "行次", "年初数", "期末数", "负债", "行次", "年初数", "期末数"],
        ["流动资产：", "", "", "", "流动负债：", "", "", ""],
        ["货币资金", "1", "100.00", "200.00", "短期借款", "68", "100.00", ""],
        ["递延税项：", "", "", "", "未分配利润", "121", "300.00", "400.00"],
    ]
    modified, text = _fill_table_from_grid(table_html, ocr_grid)

    assert "递延税项：" not in text, f"全角「递延税项：」不应被复制填充，实际 {text!r}"
    assert "流动资产：" not in text, f"全角「流动资产：」不应被复制填充，实际 {text!r}"
    assert "流动负债：" not in text, f"全角「流动负债：」不应被复制填充，实际 {text!r}"
    assert modified is False, "全部 OCR 文本均为重复标签，不应产生任何修改"


def test_fill_empty_cells_places_new_text():
    """新增文本（VLM 漏掉的内容）仍应被"放置"进空单元格（去重不误伤新信息）。

    回归保护：全表规范化去重只跳过 VLM 已含的标签，不应把真正新增的文本也拦掉。
    """
    table_html = """<table>
      <tr><td>项目</td><td>金额</td><td>备注</td></tr>
      <tr><td>营业收入</td><td>100.00</td><td></td></tr>
    </table>"""
    ocr_grid = [
        ["项目", "金额", "备注"],
        ["营业收入", "100.00", "主营业务"],  # "主营业务"为 VLM 漏掉的新增文本
    ]
    modified, text = _fill_table_from_grid(table_html, ocr_grid)

    assert "主营业务" in text, f"新增文本「主营业务」应被放置进空单元格，实际 {text!r}"
    assert modified is True, "存在新增文本时应对表格做出修改"


def test_fill_empty_cells_places_repeated_text_value():
    """跨行重复文本值（如单位「吨」）在 VLM 漏掉某一行时仍应被"放置"进空单元格。

    回归保护：去重范围只应覆盖「同行 + 表头行」，不应扩展到其它数据行——否则
    单位「吨」这类可合法重复出现的值会因全表去重被误拦，导致漏识别单元格无法恢复。
    """
    table_html = """<table>
      <tr><td>项目</td><td>单位</td><td>金额</td></tr>
      <tr><td>水费</td><td>吨</td><td>100.00</td></tr>
      <tr><td>电费</td><td></td><td>200.00</td></tr>
    </table>"""
    ocr_grid = [
        ["项目", "单位", "金额"],
        ["水费", "吨", "100.00"],
        ["电费", "吨", "200.00"],  # OCR 读到第二行也是「吨」
    ]
    modified, text = _fill_table_from_grid(table_html, ocr_grid)

    assert "吨" in text, f"重复文本值「吨」应被放置进漏识别单元格，实际 {text!r}"
    assert modified is True, "存在漏识别的重复值时应对表格做出修改"


def test_fill_empty_cells_skips_truncated_numbering_label():
    """丢失序号前缀的截断标签不应被复制填充（原则 1：输出不多不少）。

    现金流量表式场景：VLM 已含「一、经营活动产生的现金流量:」（分区标题），
    OCR 因序号「一」独立成框丢失而读出截断的「、经营活动产生的现金流量：」
    （残留「、」+ 全角冒号）。旧逻辑只做规范化后精确相等，截断标签逃过去重
    被填进空的行次列，产生重复字段。
    """
    table_html = """<table>
      <tr><td>项目</td><td>行次</td><td>金额</td><td>补充资料</td><td>行次</td><td>金额</td></tr>
      <tr><td>一、经营活动产生的现金流量:</td><td></td><td></td><td>1、将净利润调节为经营活动现金流量:</td><td></td><td></td></tr>
    </table>"""
    ocr_grid = [
        ["项目", "行次", "金额", "补充资料", "行次", "金额"],
        ["、经营活动产生的现金流量：", "", "", "、将净利润调节为经营活动现金流量：", "", ""],
    ]
    modified, text = _fill_table_from_grid(table_html, ocr_grid)

    assert "、经营活动产生的现金流量：" not in text, (
        f"截断标签「、经营活动产生的现金流量：」不应被复制填充，实际 {text!r}"
    )
    assert modified is False, "全部 OCR 文本均为截断重复标签，不应产生任何修改"


def test_fill_empty_cells_skips_trailing_truncated_label():
    """尾字截断的标签不应被复制填充（子串去重覆盖行尾截断）。

    现金流量表式场景：VLM 已含「支付的其他与筹资活动有关的现金」，
    OCR 识别丢末字「金」得「支付的其他与筹资活动有关的现」。旧逻辑精确
    相等无法判重，截断标签被填进金额列。
    """
    table_html = """<table>
      <tr><td>项目</td><td>行次</td><td>金额</td></tr>
      <tr><td>支付的其他与筹资活动有关的现金</td><td>52</td><td></td></tr>
    </table>"""
    ocr_grid = [
        ["项目", "行次", "金额"],
        ["支付的其他与筹资活动有关的现", "52", ""],
    ]
    modified, text = _fill_table_from_grid(table_html, ocr_grid)

    assert modified is False, (
        f"截断标签不应产生任何修改（不应被填进金额列），实际 {text!r}"
    )


def test_is_financial_statement_table():
    """表头含「行次」或「附注编号」列的表格应判定为财务报表样式，否则不判定。"""
    # 财务报表样式：含「行次」列
    fs_html = """<table>
      <tr><td>项目</td><td>行次</td><td>金额</td></tr>
      <tr><td>一、经营活动产生的现金流量:</td><td></td><td></td></tr>
    </table>"""
    assert _is_financial_statement_table(_table_from_html(fs_html)) is True, (
        "含「行次」表头应判定为财务报表样式"
    )

    # 兼容 VLM 输出「行 次」（关键词内部含空格）
    fs_spaced_html = """<table>
      <tr><td>项目</td><td>行 次</td><td>金额</td></tr>
    </table>"""
    assert _is_financial_statement_table(_table_from_html(fs_spaced_html)) is True, (
        "「行 次」应判定为财务报表样式"
    )

    # 财务报表样式：含「附注编号」列（会企01/02表资产负债表/利润表）
    fs_notes_html = """<table>
      <tr><td>资产</td><td>附注编号</td><td>2022年12月31日</td><td>2021年12月31日</td></tr>
      <tr><td>货币资金</td><td>七、(二)</td><td>7,633,280.89</td><td>392,045.88</td></tr>
    </table>"""
    assert _is_financial_statement_table(_table_from_html(fs_notes_html)) is True, (
        "含「附注编号」表头应判定为财务报表样式"
    )

    # 非财务报表：无「行次」/「附注编号」列
    non_fs_html = """<table>
      <tr><td>项目</td><td>单位</td><td>金额</td></tr>
      <tr><td>水费</td><td>吨</td><td>100.00</td></tr>
    </table>"""
    assert _is_financial_statement_table(_table_from_html(non_fs_html)) is False, (
        "无「行次」/「附注编号」表头不应判定为财务报表样式"
    )


def test_split_concatenated_row_deterministically_preserves_all_values():
    """确定性拆分：VLM 拼接行应拆回 2 数据行 + 1 合计行，且数值完整无丢失。

    回归保护：VLM 将「居民生活 + 生产」两张明细拼接为单行时（数量
    "2892.0015910.00"、单价 "1.407766251722.11650471401" 等），旧 OCR
    重建会丢失 15910.00 / 2.11650471401 并错位金额/税率列。确定性拆分
    应直接按数据行数 N 拆回，保留全部数值且表头 colspan 对齐。
    """
    table_html = """<table>
      <tr><td colspan="2">货物或应税劳务、服务名称*水冰雪*1-居民生活*水冰雪*5-生产合 计</td>
          <td>规格型号</td><td>单位吨吨</td>
          <td>数量2892.0015910.00</td>
          <td colspan="2">单价1.407766251722.11650471401</td>
          <td>金额4071.2633673.59¥37744.85</td>
          <td>税率3%3%</td><td>税额122.141010.21¥1132.35</td></tr>
    </table>"""
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    vlm_data, _ = _parse_vlm_table_structure(table.find_all("tr"))

    ok = _split_concatenated_row_deterministically(soup, table, vlm_data, 0)

    assert ok is True, "确定性拆分应成功"
    text = table.get_text()
    # 值保真：关键数值（含曾被 OCR 重建丢失的 15910.00 / 2.11650471401）必须齐全
    for expected in ("2892.00", "15910.00", "1.40776625172", "2.11650471401",
                     "4071.26", "33673.59", "3%", "122.14", "1010.21",
                     "¥37744.85", "¥1132.35"):
        assert expected in text, f"确定性拆分丢失值 {expected!r}，实际 {text!r}"
    # 行拆分：两条服务名称各自独立成行，合计单列一行
    assert text.count("居民生活") == 1
    assert text.count("生产") == 1
    assert "合计" in text


if __name__ == "__main__":
    test_equity_statement_label_only_rows_not_treated_as_header()
    test_simple_header_and_data()
    test_no_header_first_row_is_data()
    test_image_row_terminates_header()
    test_is_structurally_sparse_table()
    test_ocr_rebuild_recovers_amount_summary_yen()
    test_ocr_rebuild_summary_yen_unchanged_when_template_has_both()
    test_fill_empty_cells_skips_fullwidth_duplicate_labels()
    test_fill_empty_cells_places_new_text()
    test_fill_empty_cells_places_repeated_text_value()
    test_fill_empty_cells_skips_truncated_numbering_label()
    test_fill_empty_cells_skips_trailing_truncated_label()
    test_is_financial_statement_table()
    test_split_concatenated_row_deterministically_preserves_all_values()
    print("✅ 所有 _detect_data_row_start / 稀疏门控 / OCR 重建 / 去重 回归测试通过")
