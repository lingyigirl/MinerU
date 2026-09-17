#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试表格 OCR 空单元格填充的内容守卫（守卫 1/2/3/4/5）。

覆盖 特定文档类型优化规范.md 原则 1（输出不多不少）：
滕悦银行流水中「对公收费」行的 对方户名 列源 PDF 为空，VLM 正确输出
<td></td>，但 OCR 补充阶段曾把 4 类垃圾值灌入空列：

- A: 相邻格合并超集（"01-02对公收费"、"01-06收"）→ 守卫 1 双向子串去重
- B: 数字灌文本列（".0"、"0.0"、"71,212.25000580000"）→ 守卫 2 类型守卫
- C: 标点/表头截断单字（"。"、"类"）→ 守卫 3 标点/单字排除
- D: 跨列合并数字垃圾（"936.471.220005800001"）→ 守卫 1 + 守卫 2
- E: 跨行拼接噪音（"01-06收"、"2025-02-1416:53:28"）→ 守卫 4 拼接噪音
    落到错误行池 / 丢空格日期时间，守卫 1/2/3 均 MISS → 守卫 4 兜底
- F: 表格外语义源（印章文本/页标题截断单字/相邻行截断 fragment）→ 守卫 5
    语义源过滤（chrome），守卫 1-4 内容模式无可区分特征时按来源拦截：
    p1「业务专用章」= 印章行包含、p4「本庄三八支行」= 印章 OCR 误识
    编辑距离 1、p3「中」= 单 CJK ∈ 本页标题、p0「州英华…」= 同表前缀截断

同时回归保护：真实公司名、发票漏识别值（13%/28973.82）、跨行重复值「吨」、
合法短日期/带空格时间仍应正常落位（守卫不误伤）。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from bs4 import BeautifulSoup

from mineru.utils.custom.table_utils import (
    _cjk_count,
    _collect_doc_seals,
    _collect_page_title,
    _compact_norm,
    _edit_distance_le1,
    _fill_empty_cells_from_ocr_grid,
    _is_merged_noise,
    _is_pure_punctuation,
    _is_single_cjk_char,
)

# 滕悦银行流水表头（10 列）
_STMT_HEADER = [
    "日期", "业务产品种类", "凭证种类", "凭证号", "对方户名",
    "摘要", "借方发生额", "贷方发生额", "余额", "记账信息",
]


def _stmt_table_html(
    date: str, fee_label: str = "对公收费", counterparty: str = "",
) -> str:
    """构造对公收费行表格 HTML（对方户名列可空）。

    Args:
        date: 交易日期（如 "01-02"）。
        fee_label: 业务产品种类（默认 "对公收费"）。
        counterparty: 对方户名列内容（默认空 = VLM 漏识别场景）。

    Returns:
        表格 HTML 字符串。
    """
    return f"""<table>
      <tr><th>{'</th><th>'.join(_STMT_HEADER)}</th></tr>
      <tr><td>{date}</td><td>{fee_label}</td><td>0</td><td>0</td>
          <td>{counterparty}</td><td>跨行汇款手续费</td>
          <td>600.00</td><td>0.00</td><td>2,047,754.23</td><td>0005800001</td></tr>
    </table>"""


def _fill(
    table_html: str,
    ocr_row: list[str],
    header_row: list[str] | None = None,
) -> tuple[bool, str]:
    """运行 _fill_empty_cells_from_ocr_grid 并返回对方户名列文本。

    Args:
        table_html: 表格 HTML。
        ocr_row: 数据行 OCR 文本（10 列，缺省补空）。
        header_row: OCR 表头行（默认用 _STMT_HEADER）。

    Returns:
        (modified, col4_text)。
    """
    header_row = header_row or _STMT_HEADER
    row = list(ocr_row)
    while len(row) < 10:
        row.append("")
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    modified = _fill_empty_cells_from_ocr_grid(soup, table, [header_row, row])
    cells = [c.get_text().strip() for c in table.find_all("tr")[1].find_all("td")]
    return modified, cells[4]


def test_guard1_merged_adjacent_cells_superset_dropped() -> None:
    """模式 A：OCR 把「日期+业务种类」读成单框超集时不得复制进空列。

    源 PDF 「对公收费」行 对方户名为空，OCR 却输出 "01-02对公收费"
    （日期 "01-02" 与业务种类 "对公收费" 拼成的超集）。旧代码只做
    ot ∈ vt 单向子串去重，超集逃过判重被填入空列，违反原则 1。
    守卫 1 反向子串去重应丢弃该 token。
    """
    html = _stmt_table_html("01-02")
    modified, col4 = _fill(
        html,
        ["01-02", "对公收费", "01-02对公收费", "跨行汇款手续费",
         "600.00", "0.00", "2,047,754.23", "0005800001"],
    )
    assert col4 == "", f"对方户名应保持为空，实际 {col4!r}"


def test_guard1_merged_truncated_superset_dropped() -> None:
    """模式 A-变体：OCR 超集被截断（"01-06收"）时仍应丢弃。

    实际观测值：跨格合并后仅保留日期 + 业务种类首字 "01-06收"，
    内含 VLM 同格日期 "01-06"（≥4 字）→ 反向去重命中。
    """
    html = _stmt_table_html("01-06")
    modified, col4 = _fill(
        html,
        ["01-06", "对公收费", "01-06收", "跨行汇款手续费",
         "80.00", "0.00", "127,474.23", "0005800001"],
    )
    assert col4 == "", f"对方户名应保持为空，实际 {col4!r}"


def test_guard2_number_tokens_never_into_text_column() -> None:
    """模式 B/D：number 类 OCR token 不得落入表头推断为 text 的空列。

    实测垃圾值：".0"、"0.0"、"200.0"（模式 B）及跨列合并数字
    "71,212.25000580000"、"2.003.850.050005800001"（模式 D）。
    对方户名列 表头无 金额/税额/数量 关键词 → 推断为 text；
    守卫 2 第二轮去通配（mode=="empty" 不再放行所有类型），
    第三轮兜底 number→text 跳过 → 数字 token 全部丢弃。
    """
    for junk in (".0", "0.0", "200.0", "71,212.25000580000",
                 "2.003.850.050005800001", "936.471.220005800001"):
        html = _stmt_table_html("01-02")
        modified, col4 = _fill(
            html,
            ["01-02", "对公收费", junk, "跨行汇款手续费",
             "600.00", "0.00", "2,047,754.23", "0005800001"],
        )
        assert col4 == "", f"数字垃圾 {junk!r} 不得进入文本列，实际 {col4!r}"


def test_guard3_punctuation_and_header_fragment_dropped() -> None:
    """模式 C：纯标点「。」与表头截断单字「类」不得落位。

    「。」全为标点字符 → 守卫 3-① 纯标点直接丢弃；
    「类」为单个 CJK 字符且被表头「业务产品种类」包含 → 守卫 3-② 判为
    表头截断噪声丢弃。
    """
    for junk in ("。", "类"):
        html = _stmt_table_html("01-02")
        modified, col4 = _fill(
            html,
            ["01-02", "对公收费", junk, "跨行汇款手续费",
             "600.00", "0.00", "2,047,754.23", "0005800001"],
        )
        assert col4 == "", f"噪声 {junk!r} 不得进入空列，实际 {col4!r}"


def test_guard1_real_company_name_still_fills() -> None:
    """回归：真正的新增内容（公司名）仍应落位（守卫不误伤新信息）。

    VLM 漏识别的真实对方户名 "山东滕建投资集团兴唐工程有限公司"
    不含任何 VLM 同格值子串、类型为 text、长度远大于 1 → 三守卫均不拦截。
    """
    name = "山东滕建投资集团兴唐工程有限公司"
    html = _stmt_table_html("01-05", fee_label="同城转账", counterparty="")
    modified, col4 = _fill(
        html,
        ["01-05", "同城转账", name, "往来款", "0.00",
         "5,000,000.00", "6,628,354.23", "0005800033"],
    )
    assert col4 == name, f"真实公司名应被放置进空列，实际 {col4!r}"


def test_guard2_does_not_block_invoice_fills() -> None:
    """回归：发票漏识别值仍按类型落位（守卫 2 不误伤类型一致填充）。

    发票表头 金额→number、税率→rate；OCR "28973.82"（number）应填入
    金额列、"13%"（rate）应填入税率列——与表头推断类型一致，第二轮正常命中。
    """
    html = """<table>
      <tr><th>货物或应税劳务、服务名称</th><th>金额</th><th>税率</th></tr>
      <tr><td>*水冰雪*劳务</td><td>2398.25</td><td>3%</td></tr>
      <tr><td>*劳务*1类</td><td></td><td></td></tr>
    </table>"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    # OCR 行：第 3 行漏识别金额/税率，OCR 读到 28973.82 / 13%
    _fill_empty_cells_from_ocr_grid(
        soup, table,
        [["货物或应税劳务、服务名称", "金额", "税率"],
         ["*水冰雪*劳务", "2398.25", "3%"],
         ["*劳务*1类", "28973.82", "13%"]],
    )
    row = [c.get_text().strip() for c in table.find_all("tr")[2].find_all("td")]
    assert row[1] == "28973.82", f"金额列应为 28973.82，实际 {row!r}"
    assert row[2] == "13%", f"税率列应为 13%，实际 {row!r}"


def test_guard1_does_not_block_repeated_short_unit() -> None:
    """回归：跨行重复短值「吨」仍可落位（守卫 1 双侧 ≥4 字门不误伤）。

    VLM 漏识别电费行的单位「吨」，OCR 读到 「吨」（1 字 < 4）。
    反向去重要求 vt_norm/ot_norm 均 ≥4 字 → 「吨」不被吸收，
    仍按原逻辑兜底落位。
    """
    html = """<table>
      <tr><th>项目</th><th>单位</th><th>金额</th></tr>
      <tr><td>水费</td><td>吨</td><td>100.00</td></tr>
      <tr><td>电费</td><td></td><td>200.00</td></tr>
    </table>"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    _fill_empty_cells_from_ocr_grid(
        soup, table,
        [["项目", "单位", "金额"],
         ["水费", "吨", "100.00"],
         ["电费", "吨", "200.00"]],
    )
    text = table.get_text()
    assert "吨" in text, f"重复短值「吨」应仍能落位，实际 {text!r}"


def test_guard1_three_char_fragment_in_same_row_dropped() -> None:
    """模式 A-变体'：3 字后缀 fragment（「手续费」⊆「跨行汇款手续费」）拦截。

    p0 实测：守卫 5 清掉「州英华…」后，池中下一顺位「手续费」（同行摘要
    「跨行汇款手续费」的 3 字截断 fragment）顶进空对方户名。旧门槛 ≥4 字
    恰好放行 3 字 fragment——收紧为 ≥3 字后命中子串去重丢弃。
    """
    html = _stmt_table_html("01-04")
    modified, col4 = _fill(
        html,
        ["01-04", "对公收费", "手续费", "跨行汇款手续费",
         "200.00", "0.00", "2,047,554.23", "0005800001"],
    )
    assert col4 == "", f"3 字同类 fragment 不得进入空列，实际 {col4!r}"


def test_pure_punctuation_helper() -> None:
    """守卫 3 helper：纯标点判定。"""
    assert _is_pure_punctuation("。") is True
    assert _is_pure_punctuation("、") is True
    assert _is_pure_punctuation("..") is True
    assert _is_pure_punctuation("***") is False  # 打码掩码，真实内容不可丢
    assert _is_pure_punctuation("0.0") is False
    assert _is_pure_punctuation("13%") is False


def test_single_cjk_helper() -> None:
    """守卫 3 helper：单个 CJK 字符判定。"""
    assert _is_single_cjk_char("类") is True
    assert _is_single_cjk_char("吨") is True
    assert _is_single_cjk_char("对公") is False
    assert _is_single_cjk_char("0") is False


def test_guard4_merged_noise_helper() -> None:
    """守卫 4 helper：拼接噪音判定。

    命中（应丢弃）：MM-DD 紧接文字、YYYY-MM-DD 紧接时间无空格；
    不命中（真实内容）：纯日期、带空格的日期时间、纯账号、金额、百分数。
    """
    assert _is_merged_noise("01-06收") is True
    assert _is_merged_noise("01-02对公收费") is True
    assert _is_merged_noise("2025-02-1416:53:28") is True
    assert _is_merged_noise("01-06") is False
    assert _is_merged_noise("2025-02-14 16:53:28") is False  # 合法时间，空格分隔
    assert _is_merged_noise("12-3456789") is False  # 账号，`-` 后是数字
    assert _is_merged_noise("30,000,000.00") is False  # 金额，非 MM-DD 开头
    assert _is_merged_noise("13%") is False  # 税率
    assert _is_merged_noise("28973.82") is False  # 发票金额
    assert _is_merged_noise("吨") is False


def test_guard4_bug1_merged_date_with_fragment_dropped() -> None:
    """Bug 1 回归：01-06收 拼接噪音跨行落入 01-04 对公收费行时丢弃。

    真实故障：OCR 把 R29（01-06 跨行收报）的日期与 R28（对公收费）的
    「收」读成单框 "01-06收"，经 Y 聚类被分配到 01-04 对公收费行的池中。
    该噪音不含本行任何子串（守卫 1 反向去重 MISS）、非纯标点/单字
    （守卫 3 MISS）、text→text 类型一致（守卫 2 不拦）。
    守卫 4 模式 M1 应直接丢弃 → 对方户名保持空。
    """
    html = _stmt_table_html("01-04")
    modified, col4 = _fill(
        html,
        ["01-04", "对公收费", "01-06收", "跨行汇款手续费",
         "600.00", "0.00", "2,047,554.23", "0005800001"],
    )
    assert col4 == "", f"对方户名应保持为空，实际 {col4!r}"


def test_guard4_bug2_detailed_datetime_concat_dropped() -> None:
    """Bug 2 回归：明细表空列不被 2025-02-1416:53:28 拼接时间污染。

    真实故障：第 6 页明细表（表头：对方账号|交易时间|借贷标志|对方单位|
    对方行号|用途|摘要|余额|专用章转出金额|转入金额），OCR 把
    "2025-02-14" + "16:53:28" 读成单框丢空格，text 类型填入空的
    对方行号列。守卫 4 模式 M2 应直接丢弃 → 对方行号保持空。
    """
    detail_header = [
        "对方账号", "交易时间", "借贷标志", "对方单位", "对方行号",
        "用途", "摘要", "余额", "专用章转出金额", "转入金额",
    ]
    html = """<table>
      <tr><th>{}</th></tr>
      <tr><td>6236**********0033</td><td>2025-02-14 16:53:28</td>
          <td>贷</td><td>滕州社会保险</td><td></td><td>代发工资</td>
          <td>批量代发</td><td>1,234.56</td><td>0.00</td><td>2,047,754.23</td></tr>
    </table>""".format("</th><th>".join(detail_header))
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    _fill_empty_cells_from_ocr_grid(
        soup, table,
        [detail_header,
         ["6236**********0033", "2025-02-14 16:53:28", "贷",
          "滕州社会保险", "2025-02-1416:53:28", "代发工资", "批量代发",
          "1,234.56", "0.00", "2,047,754.23"]],
    )
    cells = [c.get_text().strip() for c in table.find_all("tr")[1].find_all("td")]
    assert cells[4] == "", f"对方行号应保持为空，实际 {cells[4]!r}"


def test_guard4_does_not_block_legit_dates() -> None:
    """不误伤：合法日期/带空格时间仍按原逻辑填充。

    守卫 4 只拦截拼接串：纯 "01-06"（行尾无字符，M1 不匹配）、
    "2025-02-14 16:53:28"（空格分隔，M2 不匹配）照常流入填充逻辑，
    正常落入 VLM 漏识别的 text 空列。
    """
    for legit in ("01-06", "2025-02-14 16:53:28"):
        html = """<table>
          <tr><th>交易日期</th><th>对方户名</th></tr>
          <tr><td></td><td>山东滕建投资集团兴唐工程有限公司</td></tr>
        </table>"""
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        _fill_empty_cells_from_ocr_grid(
            soup, table,
            [["交易日期", "对方户名"],
             [legit, "山东滕建投资集团兴唐工程有限公司"]],
        )
        cells = [c.get_text().strip() for c in table.find_all("tr")[1].find_all("td")]
        assert cells[0] == legit, (
            f"合法值 {legit!r} 应正常填充交易日期列，实际 {cells[0]!r}"
        )


# ---------------------------------------------------------------------------
# 守卫 5：语义源过滤（chrome）
# ---------------------------------------------------------------------------


def _fill_chrome(
    table_html: str,
    ocr_row: list[str],
    chrome: dict | None,
    header_row: list[str] | None = None,
) -> tuple[bool, str]:
    """运行带 chrome 的 _fill_empty_cells_from_ocr_grid 并返回对方户名列文本。

    Args:
        table_html: 表格 HTML。
        ocr_row: 数据行 OCR 文本（10 列，缺省补空）。
        chrome: 守卫 5 语义源上下文（None 时可退化为 3 参调用）。
        header_row: OCR 表头行（默认用 _STMT_HEADER）。

    Returns:
        (modified, col4_text)。
    """
    header_row = header_row or _STMT_HEADER
    row = list(ocr_row)
    while len(row) < 10:
        row.append("")
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    if chrome is None:
        modified = _fill_empty_cells_from_ocr_grid(soup, table, [header_row, row])
    else:
        modified = _fill_empty_cells_from_ocr_grid(
            soup, table, [header_row, row], chrome=chrome
        )
    cells = [c.get_text().strip() for c in table.find_all("tr")[1].find_all("td")]
    return modified, cells[4]


# 滕悦银行流水文档级印章（审计净剩 3 行纯公章文本）
_STMT_SEALS = {
    "中国工商银行股份有限公司",
    "枣庄三八支行",
    "业务专用章",
}


def test_guard5_seal_containment_dropped() -> None:
    """模式 F-5A1：印章文本被读入空列时丢弃（p1「业务专用章」回归）。

    真实故障：p1 R29（01-12 跨行快汇手续费）对方户名为空，页面底部印章
    第 3 行「业务专用章」经 Y 聚类错位进入该行 OCR 池。token 与合法公司名
    无内容差异，但被文档级印章集包含 → 守卫 5 按来源拦截。

    OCR 行锚点（01-12/对公收费/跨行汇款手续费）与 VLM 同格值重复被守卫 1
    吸收，印章 token「业务专用章」在对方户名位置——若未被拦则落 col4，
    被拦则 col4 保持空。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    html = _stmt_table_html("01-12")
    modified, col4 = _fill_chrome(
        html,
        ["01-12", "对公收费", "业务专用章", "跨行汇款手续费",
         "600.00", "0.00", "2,047,754.23", "0005800001"],
        chrome,
    )
    assert col4 == "", f"印章文本不得进入空列，实际 {col4!r}"


def test_guard5_seal_edit1_dropped() -> None:
    """模式 F-5A-edit：印章 OCR 误识（编辑距离 1）仍拦截（p4 回归）。

    真实故障：p4 R24（01-28 跨行汇款手续费）对方户名为空，印章第 2 行
    「枣庄三八支行」被 OCR 读成「本庄三八支行」（枣→本 单字替换）。
    与印章行编辑距离 ≤1 → 按来源拦截。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    html = _stmt_table_html("01-28")
    modified, col4 = _fill_chrome(
        html,
        ["01-28", "对公收费", "本庄三八支行", "跨行汇款手续费",
         "40.00", "0.00", "1,237,774.23", "0005800001"],
        chrome,
    )
    assert col4 == "", f"印章 OCR 误识文本不得进入空列，实际 {col4!r}"


def test_guard5_single_cjk_in_page_title_dropped() -> None:
    """模式 F-5A3：单 CJK ∈ 本页标题时丢弃（p3「中」回归）。

    真实故障：p3 R29（01-26 跨行汇款手续费）对方户名为空，页标题
    「中国工商银行对公客户账务明细」截断单字「中」落入空列。
    守卫 3-② 查表头不查标题（表头无「中」）MISS → 守卫 5 比对页标题拦截。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    html = _stmt_table_html("01-26")
    modified, col4 = _fill_chrome(html, ["01-26", "跨行汇款手续费", "中"], chrome)
    assert col4 == "", f"页标题截断单字不得进入空列，实际 {col4!r}"


def test_guard5_cross_row_truncation_dropped() -> None:
    """模式 F-5B：同表另一行的前后缀截断 fragment 丢弃（p0 回归）。

    真实故障：p0 R28（01-04 跨行汇款手续费）对方户名为空，R29（01-06
    跨行收报）摘要「滕州英华高级中学有限公司」截首字「州英华高级中学
    有限公司」经行池错位进入 R28。token 不含本行任何子串（守卫 1 MISS），
    但与同表已填 cell 前缀匹配且长度差 1 → 守卫 5 跨行截断拦截。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    # VLM HTML：01-04 行对方户名为空，01-06 行已有完整「滕州英华…」
    html = f"""<table>
      <tr><th>{'</th><th>'.join(_STMT_HEADER)}</th></tr>
      <tr><td>01-04</td><td>对公收费</td><td>0</td><td>0</td>
          <td></td><td>跨行汇款手续费</td>
          <td>200.00</td><td>0.00</td><td>2,047,554.23</td><td>0005800001</td></tr>
      <tr><td>01-06</td><td>跨行收报</td><td>0</td><td>0</td>
          <td>滕州英华高级中学有限公司</td><td>业务专用章</td>
          <td>0.00</td><td>500,000.00</td><td>2,547,554.23</td><td>0005800001</td></tr>
    </table>"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    # OCR 网格：表头行 + 01-04 行（含跨入的 fragment）+ 01-06 行
    _fill_empty_cells_from_ocr_grid(
        soup, table,
        [_STMT_HEADER,
         ["01-04", "对公收费", "州英华高级中学有限公司", "跨行汇款手续费",
          "200.00", "0.00", "2,047,554.23", "0005800001"],
         ["01-06", "跨行收报", "", "业务专用章",
          "0.00", "500,000.00", "2,547,554.23", "0005800001"]],
        chrome=chrome,
    )
    rows = table.find_all("tr")
    cells0 = [c.get_text().strip() for c in rows[1].find_all("td")]
    assert cells0[4] == "", f"跨行截断 fragment 不得进入空列，实际 {cells0[4]!r}"
    cells1 = [c.get_text().strip() for c in rows[2].find_all("td")]
    assert cells1[4] == "滕州英华高级中学有限公司", "真实完整值应保留"
    assert cells1[5] == "业务专用章", "真实摘要应保留"


def test_guard5_cross_row_merged_td_double_value_dropped() -> None:
    """模式 F-5B'：VLM 把两格合并进同一 td（空白分隔），截断 fragment 仍拦截。

    p0 实测：R29（01-06 跨行收报）vlm HTML 为
    `<td>滕州英华高级中学有限公司 业务专用章</td>`——对方户名+摘要两格被
    VLM 合并为一个 td。早期 table_cell_norms 收整值 compact 后变成
    「滕州英华高级中学有限公司业务专用章」，fragment「州英华高级中学有
    限公司」既非其前缀也非后缀（被「业务专用章」挤到中间）→ 5B MISS。
    修复：按空白拆段各自入集，前缀匹配恢复 → 拦截。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    html = f"""<table>
      <tr><th>{'</th><th>'.join(_STMT_HEADER)}</th></tr>
      <tr><td>01-04</td><td>对公收费</td><td>0</td><td>0</td>
          <td></td><td>跨行汇款手续费</td>
          <td>200.00</td><td>0.00</td><td>2,047,554.23</td><td>0005800001</td></tr>
      <tr><td>01-06</td><td>跨行收报</td><td>0</td><td>0</td>
          <td>滕州英华高级中学有限公司 业务专用章</td><td>往来款</td>
          <td>0.00</td><td>2,000,000.00</td><td>4,047,554.23</td><td>0077600495</td></tr>
    </table>"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    _fill_empty_cells_from_ocr_grid(
        soup, table,
        [_STMT_HEADER,
         ["01-04", "对公收费", "州英华高级中学有限公司", "跨行汇款手续费",
          "200.00", "0.00", "2,047,554.23", "0005800001"],
         ["01-06", "跨行收报", "", "往来款",
          "0.00", "2,000,000.00", "4,047,554.23", "0077600495"]],
        chrome=chrome,
    )
    rows = table.find_all("tr")
    cells0 = [c.get_text().strip() for c in rows[1].find_all("td")]
    assert cells0[4] == "", f"合并 td 场景下截断 fragment 仍应被拦，实际 {cells0[4]!r}"
    cells1 = [c.get_text().strip() for c in rows[2].find_all("td")]
    assert cells1[4] == "滕州英华高级中学有限公司 业务专用章", "VLM 合并值应原样保留"


def test_guard5_real_values_still_fill() -> None:
    """回归：真实公司名（非印章/非标题/无同表截断）仍正常落位。

    真值「山东滕建投资集团兴唐工程有限公司」不含任何印章行、不在页标题、
    也非同表 cell 的前后缀截断 → 守卫 5 不拦截，正常填入空列。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    name = "山东滕建投资集团兴唐工程有限公司"
    html = _stmt_table_html("01-05", fee_label="同城转账", counterparty="")
    modified, col4 = _fill_chrome(
        html,
        ["01-05", "同城转账", name, "往来款", "0.00",
         "5,000,000.00", "6,628,354.23", "0005800033"],
        chrome,
    )
    assert col4 == name, f"真实公司名应正常落位，实际 {col4!r}"


def test_guard5_short_or_far_truncation_not_dropped() -> None:
    """不误伤：短截断（长度差大 / token 过短）不拦截。

    守卫 5B 收紧点：长度差 ≤2 排除「账号…上页余额 674620049」型超长超集
    （差 3 字），len≥5 排除「付材料款」⊆「支付材料款」（4 字，且长度差 1）。
    """
    chrome = {"seals": _STMT_SEALS, "title": "中国工商银行对公客户账务明细"}
    # 表内另一行（付款行）含「支付材料款」，OCR 池含「付材料款」→ len<5 不拦截
    html = f"""<table>
      <tr><th>{'</th><th>'.join(_STMT_HEADER)}</th></tr>
      <tr><td>01-04</td><td>对公收费</td><td>0</td><td>0</td>
          <td></td><td>跨行汇款手续费</td>
          <td>200.00</td><td>0.00</td><td>2,047,554.23</td><td>0005800001</td></tr>
      <tr><td>01-05</td><td>同城转账</td><td>0</td><td>0</td>
          <td>山东滕建投资集团</td><td>支付材料款</td>
          <td>0.00</td><td>5,000,000.00</td><td>6,628,354.23</td><td>0005800033</td></tr>
    </table>"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    _fill_empty_cells_from_ocr_grid(
        soup, table,
        [_STMT_HEADER,
         ["01-04", "对公收费", "付材料款", "跨行汇款手续费",
          "200.00", "0.00", "2,047,554.23", "0005800001"],
         ["01-05", "同城转账", "", "支付材料款",
          "0.00", "5,000,000.00", "6,628,354.23", "0005800033"]],
        chrome=chrome,
    )
    rows = table.find_all("tr")
    cells0 = [c.get_text().strip() for c in rows[1].find_all("td")]
    # 「付材料款」4 字 < 5 → 5B 不拦截 → 按原逻辑落位（真实短值保护）
    assert cells0[4] == "付材料款", f"短截断值应仍可落位，实际 {cells0[4]!r}"


def test_guard5_chrome_none_noop() -> None:
    """回归：chrome=None（3 参旧调用）守卫 5 不生效，行为不变。

    印章文本在无 chrome 时仍会按原逻辑填入（守卫 5 是增量过滤，默认关闭），
    保证既有调用方（及其他测试的 3 参调用）行为完全不变。
    """
    html = _stmt_table_html("01-12")
    modified, col4 = _fill_chrome(
        html,
        ["01-12", "对公收费", "业务专用章", "跨行汇款手续费",
         "600.00", "0.00", "2,047,754.23", "0005800001"],
        None,
    )
    assert col4 == "业务专用章", f"无 chrome 时守卫 5 不拦截，实际 {col4!r}"


def test_guard5_helpers() -> None:
    """守卫 5 helper：规范化 / CJK 计数 / 编辑距离 ≤1 判定。"""
    assert _compact_norm(" 枣庄 三八支行 ") == "枣庄三八支行"
    assert _compact_norm("中国工商银行股份有限公司") == "中国工商银行股份有限公司"
    assert _compact_norm("") == ""
    assert _cjk_count("中国工商银行") == 6
    assert _cjk_count("abc123") == 0
    assert _cjk_count("业务专用章45F604CE4024") == 5  # 数字不算 CJK
    assert _edit_distance_le1("枣庄三八支行", "本庄三八支行") is True  # 枣→本 替换
    assert _edit_distance_le1("业务专用章", "业务专用章") is True  # 相同
    assert _edit_distance_le1("业务专用章", "业务专用章2") is True  # 1 次插入
    assert _edit_distance_le1("往来款", "中国人民银行") is False
    assert _edit_distance_le1("枣庄三八支行", "中国工商银行股份有限公司") is False
    assert _edit_distance_le1("中国工商银行股份有限公司", "枣庄三八支行") is False


# 滕悦银行流水 p0 中间 JSON（含 image 块，印章 3 行 + 编号戳）；构造仅 args 需要的字段。
# 注意：真实 middle.json 中 image span 挂在 image_body 子块的 lines 下，
# iter_block_spans 先遍历 lines 再递归 blocks——此结构与 _collect_doc_seals 的实现对应。
_P0_PAGE = {
    "page_idx": 0,
    "preproc_blocks": [
        {"type": "title", "bbox": [0, 0, 100, 20],
         "lines": [{"spans": [{"type": "text",
                               "content": "中国工商银行对公客户账务明细"}]}]},
        {"type": "table", "bbox": [0, 40, 100, 200],
         "blocks": [{"type": "table_body", "bbox": [0, 40, 100, 200]}]},
        {"type": "image", "bbox": [0, 250, 100, 280],
         "blocks": [{"type": "image_body",
                     "lines": [
                         {"spans": [{"type": "image",
                                     "content": "中国工商银行股份有限公司\n"
                                                 "枣庄三八支行\n业务专用章\n"
                                                 "45F604CE4024"}]},
                     ]}]},
    ],
}


def test_guard5_collect_doc_seals_image_spans() -> None:
    """守卫 5 采集：跨页 image span 提取印章行，排除签名/日期戳。

    真实 p0 image 项：content 为多行文本（纯公章 3 行 + 编号行）。
    仅 CJK≥4 且无数字的行进入印章集——「45F604CE4024」含数字被排除。
    """
    seals = _collect_doc_seals([_P0_PAGE])
    assert "业务专用章" in seals, f"公章行应被采集，实际 {seals!r}"
    assert "枣庄三八支行" in seals, f"公章行应被采集，实际 {seals!r}"
    assert "中国工商银行股份有限公司" in seals, f"公章行应被采集，实际 {seals!r}"
    assert not any("45F604CE4024" in s for s in seals), "含数字行不得进入印章集"


def test_guard5_collect_page_title_caption() -> None:
    """守卫 5 采集：表格上方 text 块标题被归一化为页标题。

    真实 p3 无 table_caption，标题来自表格上方的 text 块（bbox y0 < 表格 y0）。
    ≥10 字且 CJK 居多的文本 → 规范化页标题。
    """
    page = {"page_idx": 3, "preproc_blocks": [
        {"type": "text", "bbox": [0, 0, 100, 20],
         "lines": [{"spans": [{"type": "text",
                               "content": "中国工商银行对公客户账务明细"}]}]},
        {"type": "table", "bbox": [0, 40, 100, 200],
         "blocks": [{"type": "table_body", "bbox": [0, 40, 100, 200]}]},
    ]}
    title = _collect_page_title(page)
    assert title == "中国工商银行对公客户账务明细", f"实际 {title!r}"


def test_guard5_collect_page_title_none() -> None:
    """守卫 5 采集：无表格块（或表格上方无标题）时返回空串。"""
    assert _collect_page_title({"page_idx": 9, "preproc_blocks": []}) == ""
    page = {"page_idx": 7, "preproc_blocks": [
        {"type": "text", "bbox": [0, 200, 100, 220],   # 在表格下方，不算标题
         "lines": [{"spans": [{"type": "text",
                               "content": "中国工商银行对公客户账务明细"}]}]},
        {"type": "table", "bbox": [0, 40, 100, 200],
         "blocks": [{"type": "table_body", "bbox": [0, 40, 100, 200]}]},
    ]}
    assert _collect_page_title(page) == "", "表格下方文本不算页标题"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-o", "addopts="]))
