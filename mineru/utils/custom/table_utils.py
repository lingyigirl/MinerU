"""VLM 表格 HTML 后处理工具。

针对 VLM 模型（MinerU2.5-Pro）在 hybrid 后端生成的表格 HTML 中，
数据行被错误地合并为单个 colspan 单元格的问题（如增值税发票中的
"项目名称 规格型号 ... 税额 数据 数据 ..."），进行自动检测和拆分。

上游合并时此模块仅需保留，无需修改。
"""

import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString, Tag
from loguru import logger

# ============================================================================
# 发票处理函数地图（维护索引）
# ============================================================================
# 本模块与增值税发票后处理相关的函数分属两个阶段，共用多套关键词常量：
#
# 【阶段B：内容生成钩子】入口 _format_embedded_html（vlm_middle_json_mkcontent.py），
# 按固定顺序串行调用（序号即钩子顺序）：
#   1. split_merged_table_cells        全行/局部合并单元格拆分（:26）
#   2. split_summary_from_data_cell    数据行内嵌「合计」拆分 + rowspan 处理（:1022）
#   3. extract_column_header_prefixes  表头前缀提取 / ¥ 对齐（:2386）
#   4. normalize_invoice_table         发票专用规范化入口（:2149），内部依次：
#        _normalize_vat_invoice_columns  8 列签名归一化（名称/单价 colspan 2→1，:2036）
#        _format_summary_row_colspan     合计行连续空单元格合并（:1684）
#        _infer_missing_values_in_table  缺失税率推断（默认关闭，:1752）
#   5. fix_summary_row_yen_position    合计行 ¥/￥ 值列对齐（:2218）
#   6. split_info_cell_multiline       购买方/销售方信息多行拆分（:1875）
#
# 【阶段A：Hybrid OCR 补充】入口 finalize_middle_json
# （hybrid_model_output_to_middle_json.py）：
#   supplement_vlm_table_cells_with_ocr  VLM 表格空单元格 OCR 补充 / 拼接行确定性重建（:4384）
#   supplement_empty_table_cells         通用空单元格 OCR 补充（:2723）
#
# 【发票检测与归一化】
#   _is_invoice_table                发票表级检测（≥3 关键词，:2579）
#   _normalize_vat_invoice_columns   8 列签名收拢（专用/普通发票，:2036）
#   _is_structurally_sparse_table / _is_financial_statement_table  反向门控（:2634/:2660）
#
# 【共享常量】（关键词集合语义有重叠，维护时注意同步）
#   _INVOICE_HEADER_KEYWORDS        通用发票表头关键词（:17）
#   _SPLIT_SUMMARY_KEYWORDS         合计/小计/总计摘要关键词（:1019）
#   _INVOICE_DETECTION_KEYWORDS     发票检测关键词（:2573）
#   _INVOICE_DATA_COLUMN_KEYWORDS   数据列关键词（:3126）
#   _NUMERIC_COLUMN_KEYWORDS        数值列关键词（:3168）
#   _VAT_INVOICE_NAME_LABELS        货物区名称列标签（:1975）
#   _VAT_INVOICE_COLUMN_SIGNATURE   8 列签名（除名称外 7 列，:1978）
#   _VAT_INVOICE_ROW_LABELS         非货物区行级标签（:1981）
#
# 注：行号为维护时的近似值，以 grep 实际位置为准；本索引只标注职责与调用链。

# 常见发票表头关键词集合（用于识别合并单元格文本中的表头部分）
_INVOICE_HEADER_KEYWORDS = {
    "项目名称", "货物或应税劳务、服务名称", "规格型号", "单位", "数量",
    "单价", "金额", "税率", "税额", "税率/征收率",
    "价税合计", "合计", "备注", "购买方", "销售方",
    "名称", "纳税人识别号", "地址", "电话", "开户银行",
    "银行账号", "收款人", "复核人", "开票人",
}


def split_merged_table_cells(html: str) -> str:
    """检测并拆分 VLM 表格中被错误合并的 colspan 单元格。

    当表格中某行仅有一个 <td> 且其 colspan 覆盖大部分列，
    同时单元格文本表现为"表头标签 + 数据值"的拼接模式时，
    利用内嵌的表头标签序列将其拆分为独立的 <td> 单元格。

    还处理以下边界情况：
    - 括号注释错位修正（如 "(度)" 从数值列移回单位列）
    - 合计行拆分（如 "合计 ¥X ¥Y" 按列签名对齐到金额和税额列）

    Args:
        html: 表格 HTML 字符串。

    Returns:
        拆分后的 HTML 字符串；若检测失败或拆分结果不符合预期，
        返回原始 HTML 不做修改。
    """
    if not html or not isinstance(html, str):
        return html

    if "<table" not in html.lower():
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过拆分")
        return html

    modified = False
    for table in soup.find_all("table"):
        try:
            rows = table.find_all("tr")
            if len(rows) < 1:
                continue

            total_columns = _compute_total_columns(rows)
            if total_columns < 3:
                continue

            # 列签名：记录第一行成功拆分后的各列类型
            # ["text", "text", "text", "number", "number", "number", "number", "number"]
            # 供合计行等摘要行做右对齐参考
            column_signature: Optional[list[str]] = None
            # 主数据行的值（用于合计行中数值与列的精确匹配）
            reference_row_values: Optional[list[str]] = None
            # 表头标签（第一行成功拆分后获取，供后续插入 <th> 行使用）
            saved_header_labels: list[str] = []

            for row in rows:
                if not _is_merged_row(row, total_columns):
                    # 非全行合并 → 尝试拆分部分合并的单元格
                    # （如 VLM 将末尾 3 列合并为 1 个 td："金额 税率/征收率 税额 1769.91 13% 230.09"）
                    if _try_split_partial_merge(row, soup, total_columns):
                        modified = True
                        # 拆分后重新计算总列数
                        total_columns = _compute_total_columns(rows)
                    continue

                cell = row.find(["td", "th"])
                merged_text = cell.get_text().strip()
                if not merged_text:
                    continue

                # 从合并单元格文本中提取表头标签和列数据值
                header_labels, split_values = _split_merged_cell_text(
                    merged_text, total_columns, column_signature,
                    reference_row_values,
                )
                if split_values is None:
                    continue

                # 校验列数：拆分结果至少等于 total_columns 的 70%
                # 允许拆分结果 > total_columns（VLM 的 colspan 可能不准确）
                actual_columns = len(split_values)
                if actual_columns < max(3, total_columns * 0.7):
                    continue

                # 如果拆分列数 > 原先的 total_columns，以拆分为准
                if actual_columns > total_columns:
                    total_columns = actual_columns

                # 修正括号注释错位（如 "(度)" 从税额列移回单位列）
                split_values = _fix_parenthetical_annotations(split_values)

                # 用拆分后的值创建新 <td> 替换原合并单元格
                new_tds = []
                for value in split_values:
                    new_td = soup.new_tag("td")
                    new_td.string = value
                    new_tds.append(new_td)

                cell.replace_with(*new_tds)

                # 记录第一行数据行的列签名和值，供后续摘要行对齐
                if column_signature is None:
                    column_signature = [
                        "number" if _is_data_value(v) else "text"
                        for v in split_values
                    ]
                    reference_row_values = split_values

                # 保存表头标签并在需要时插入 <th> 行
                if header_labels and len(header_labels) >= 3:
                    saved_header_labels = header_labels

                if saved_header_labels and not _has_header_row(table, total_columns):
                    _insert_header_row(soup, row, saved_header_labels)

                modified = True
                logger.debug(
                    f"表格单元格拆分成功：{total_columns}列，"
                    f"原文前60字={merged_text[:60]}..."
                )

        except Exception:
            logger.exception("处理单个表格时出错，跳过此表格")
            continue

    if modified:
        return str(soup)
    return html


def _try_split_partial_merge(
    row: Tag,
    soup: BeautifulSoup,
    total_columns: int,
) -> bool:
    """检测并拆分表格行中部分合并的单元格（非全行合并场景）。

    当一行有多个单元格，但其中某个单元格包含 "表头标签 + 数据值"
    的拼接文本时（如 VLM 输出 "金额 税率/征收率 税额 1769.91 13% 230.09"），
    将表头标签替换原单元格位置为新 <th>，数据值移至下一行（数据行）。

    Args:
        row: BeautifulSoup <tr> Tag。
        soup: BeautifulSoup 对象，用于创建新标签。
        total_columns: 当前表格总列数。

    Returns:
        True 表示有修改（至少拆分了一个单元格）。
    """
    cells = row.find_all(["td", "th"])
    if len(cells) < 2:
        return False

    modified = False

    for cell_idx, cell in enumerate(cells):
        text = cell.get_text().strip()
        if not text:
            continue

        tokens = text.split()
        if len(tokens) < 4:
            continue

        # 用 _classify_tokens 区分表头 token 和数据 token
        header_labels, data_values = _classify_tokens(tokens)
        if header_labels is None or data_values is None:
            continue

        num_header = len(header_labels)
        num_data = len(data_values)

        # 至少需要 2 个表头 + 2 个数据才认为是有意义的合并
        if num_header < 2 or num_data < 2:
            continue

        logger.info(
            f"部分合并单元格拆分: row={cell_idx}, "
            f"文本=\"{text[:80]}...\" → "
            f"{num_header}个表头 + {num_data}个数据"
        )

        # 将原合并单元格替换为表头 <th> 标签
        new_header_cells = []
        for header in header_labels:
            new_th = soup.new_tag("th")
            new_th.string = header
            new_header_cells.append(new_th)

        cell.replace_with(*new_header_cells)
        modified = True

        # 将数据值移至下一行（数据行）对应列
        next_row = row.find_next_sibling("tr")
        if next_row and data_values:
            _fill_data_row_columns(next_row, cell_idx, data_values, soup)

    return modified


def _fill_data_row_columns(
    data_row: Tag,
    merge_col_idx: int,
    data_values: list[str],
    soup: BeautifulSoup,
) -> None:
    """将数据值填入数据行中由合并单元格拆分产生的对应列。

    合并单元格在表头行位置 `merge_col_idx` 被拆分为 N 个 <th>，
    数据行需在相同列位置填入对应的 N 个数据值。

    算法：
    1. 计算 data_row 现有列的实际跨度
    2. 若不足 merge_col_idx 则补空 <td>
    3. 覆盖 merge_col_idx 处开始的空单元格（若该位置被 colspan 覆盖则追加）

    Args:
        data_row: 数据行 <tr> Tag。
        merge_col_idx: 合并单元格在原行中的位置索引。
        data_values: 数据值列表。
        soup: BeautifulSoup 对象。
    """
    # 获取数据行现有的所有单元格
    existing_cells = data_row.find_all(["td", "th"])

    # 计算截止 merge_col_idx 之前的列跨度
    span_before = 0
    for i, ec in enumerate(existing_cells):
        cs = int(ec.get("colspan", 1))
        if span_before + cs > merge_col_idx:
            # merge_col_idx 落在这个单元格的 colspan 范围内
            # 需要插入 data_values 到这个位置
            # 先把当前单元格之后的单元格收集起来
            after_cells = existing_cells[i + 1:]
            # 删除当前及之后的所有单元格
            for ac in existing_cells[i:]:
                ac.decompose()
            # 添加 data_values
            for val in data_values:
                new_td = soup.new_tag("td")
                new_td.string = val
                data_row.append(new_td)
            # 重新添加之后的单元格
            for ac in after_cells:
                data_row.append(ac)
            return
        span_before += cs

    # merge_col_idx 在现有所有单元格之后，补空再追加数据
    while span_before < merge_col_idx:
        empty_td = soup.new_tag("td")
        empty_td.string = ""
        data_row.append(empty_td)
        span_before += 1

    for val in data_values:
        new_td = soup.new_tag("td")
        new_td.string = val
        data_row.append(new_td)


def _compute_total_columns(rows: list[Tag]) -> int:
    """计算表格总列数（考虑 colspan 的最大扩展列数）。

    Args:
        rows: BeautifulSoup <tr> Tag 列表。

    Returns:
        总列数（整数，最小为 1）。
    """
    max_cols = 1
    for row in rows:
        col_count = 0
        for cell in row.find_all(["td", "th"]):
            colspan = int(cell.get("colspan", 1))
            col_count += colspan
        if col_count > max_cols:
            max_cols = col_count
    return max_cols


def _is_merged_row(row: Tag, total_columns: int) -> bool:
    """判断某行是否为被错误合并的单单元格行。

    条件：
    1. 该行恰好包含一个 <td> 或 <th>
    2. 其 colspan 覆盖大部分列（>= 70% 或至少 4）

    Args:
        row: BeautifulSoup <tr> Tag。
        total_columns: 表格总列数。

    Returns:
        是否为合并行。
    """
    cells = row.find_all(["td", "th"])
    if len(cells) != 1:
        return False

    colspan = int(cells[0].get("colspan", 1))
    threshold = max(4, int(total_columns * 0.7))
    return colspan >= threshold


def _split_merged_cell_text(
    merged_text: str,
    total_columns: int = 0,
    column_signature: Optional[list[str]] = None,
    reference_row_values: Optional[list[str]] = None,
) -> tuple[list[str], Optional[list[str]]]:
    """将合并单元格文本拆分为表头标签和数据值列表。

    合并单元格文本格式：
      表头1 表头2 ... 表头N 数据1 数据2 ... 数据N
    即先空白分隔地列出所有表头标签，再以空白分隔列出数据值。
    表头与数据之间可能无空格（如 "税额*供电*电费"）。

    当 column_signature 和 reference_row_values 已存在时（即第一行已拆分），
    支持拆分表头标签数量少于 3 的摘要行（如 "合计 ¥X ¥Y"），
    并通过数值匹配确定各值对应的列位置。

    算法：
    1. 空白分词
    2. 从左到右扫描，识别并跳过已知表头关键词
    3. 遇到混合 token（如 "税额*供电*电费"）时剥离表头前缀
    4. 剩余纯数据 token 按列分配（主数据行）/ 按值匹配对齐（摘要行）

    Args:
        merged_text: 合并单元格的文本内容。
        total_columns: 表格总列数（用于摘要行右对齐）。
        column_signature: 列类型签名列表 ["text"/"number"]，
            从第一行数据行获取，用于摘要行的值对齐。
        reference_row_values: 主数据行各列的值，用于摘要行数值匹配。

    Returns:
        (header_labels, data_values)。
        header_labels: 从合并文本中提取的表头标签列表。
        data_values: 分配给各列的数据值列表，或 None（拆分失败）。
    """
    tokens = merged_text.split()
    if not tokens:
        return [], None

    # 提取表头关键词序列和纯数据 token
    header_tokens, data_tokens = _classify_tokens(tokens)
    if data_tokens is None or not data_tokens:
        return header_tokens, None

    num_header = len(header_tokens)

    # 主数据行：表头标签 >= 3 个，说明是完整的表头+数据行
    if num_header >= 3:
        values = _allocate_to_columns(data_tokens, num_header)
        if values is None:
            return header_tokens, None
        # 检测并修正嵌入的合计行数值对齐
        values = _fix_embedded_summary(values, header_tokens)
        return header_tokens, values

    # 摘要行（如 "合计 ¥X ¥Y"）：表头标签 < 3 个但仍有数据
    # 需要利用 column_signature 和 reference_row_values 做对齐
    if column_signature and total_columns > 0:
        return header_tokens, _allocate_summary_row(
            header_tokens, data_tokens, total_columns,
            column_signature, reference_row_values,
        )

    return header_tokens, None


def _classify_tokens(
    tokens: list[str],
) -> tuple[list[str], Optional[list[str]]]:
    """将 token 分类为表头 token 和数据 token。

    预先处理：
    - 剥离 token 开头的标点符号（如 ".规格型号" → "规格型号"）
    - 合并组成已知多字关键词的相邻单字 token（如 "合"+"计" → "合计"）

    从左到右扫描 token 列表：
    - 纯表头 token（如 "项目名称"）→ 计入 header_tokens
    - 混合 token（如 "税额*供电*电费"）→ 剥离表头前缀，
      表头部分计入 header_tokens，数据部分计入 data_tokens
    - 纯数据 token（如 "403480"）→ 计入 data_tokens

    Args:
        tokens: 空白分词后的 token 列表。

    Returns:
        (header_tokens, data_tokens)。
    """
    # 预处理：合并被空格拆分的多字关键词（如 "合 计" → "合计"）
    tokens = _merge_split_keywords(tokens)
    # 预处理：剥离 token 开头的标点符号
    tokens = [_strip_leading_punctuation(t) for t in tokens]

    header_tokens = []
    data_tokens = []

    in_data_section = False

    for token in tokens:
        if not token:
            continue
        if in_data_section:
            data_tokens.append(token)
            continue

        # 检查是否纯表头关键词
        if token in _INVOICE_HEADER_KEYWORDS:
            header_tokens.append(token)
            continue

        # 检查是否以表头关键词开头（与数据拼接的情况）
        stripped_result = _strip_header_prefix(token)
        if stripped_result is not None:
            # 该 token 以某个已知表头关键词开头，后跟数据
            header_tokens.append(stripped_result["header"])
            if stripped_result["data"]:
                data_tokens.append(stripped_result["data"])
            # 从此 token 开始进入数据段
            in_data_section = True
            continue

        # 无法识别为表头，可能是纯数据段开始
        # 检查是否为明显的数值类型
        if _is_data_value(token):
            in_data_section = True
            data_tokens.append(token)
        else:
            # 无法明确分类的 token（非表头关键词、也非数值），
            # 可能是发票中的非数值数据（如"免税"、"***"等），作为数据保留
            in_data_section = True
            data_tokens.append(token)
            logger.debug(f"无法明确分类的 token: '{token}'，作为数据保留")

    if not header_tokens:
        return [], None

    return header_tokens, data_tokens if data_tokens else None


def _strip_header_prefix(token: str) -> Optional[dict]:
    """检查 token 是否以已知表头关键词开头，若匹配则剥离。

    VLM 对纵向排版的表头（如「数量」「单价」上下两字）会输出为「数 量」、
    「单 价」（关键词内部含空格），此时直接前缀匹配会失败。因此先按原文本
    直接前缀匹配（保留数据值内部空格），失败时再用去除全部空白后的紧凑文本
    匹配，容忍关键词内部空格（与 _match_data_column_keyword 保持一致）。

    Args:
        token: 待检查的 token。

    Returns:
        {"header": 表头关键词, "data": 剩余文本} 或 None。
    """
    # 紧凑文本：去除全部空白，用于容忍关键词内部空格（如「数 量」→「数量」）
    compact = "".join(token.split())
    for header in sorted(_INVOICE_HEADER_KEYWORDS, key=len, reverse=True):
        # 直接前缀匹配（保留数据值内部空格）
        if token.startswith(header) and len(token) > len(header):
            data = token[len(header):].strip()
            if data:
                return {"header": header, "data": data}
        # 关键词内部含空格（如「数 量1622」→「数量」+「1622」），
        # 仅在紧凑文本与原文不同且紧凑文本能前缀匹配时才用紧凑文本
        if compact != token and compact.startswith(header) and len(compact) > len(header):
            data = compact[len(header):].strip()
            if data:
                return {"header": header, "data": data}
    return None


def _strip_leading_punctuation(token: str) -> str:
    """去除 token 开头的非中文标点符号。

    场景：VLM 输出 ".规格型号" 中 "." 是表格竖线的误识别残留。

    Args:
        token: 原始 token。

    Returns:
        去除开头标点后的 token。
    """
    # 仅去除表格竖线误识别的残留符号（单个 "."），保留有意义的标点
    cleaned = re.sub(r'^\.(?=[^.\d])', '', token).strip()
    return cleaned or token


def _merge_split_keywords(tokens: list[str]) -> list[str]:
    """合并因空格拆分导致的多字关键词（如 "合"+"计" → "合计"）。

    VLM 在输出时可能在关键词内部误插空格，导致原本如 "合计" 的关键词
    被拆分为 "合" "计" 两个独立 token。此函数扫描相邻的短 token，
    若合并后能匹配已知关键词则合并。

    Args:
        tokens: 原始 token 列表。

    Returns:
        合并后的 token 列表。
    """
    if len(tokens) < 2:
        return tokens

    result = []
    i = 0
    while i < len(tokens):
        merged = tokens[i]
        j = i + 1
        # 尝试合并最多 3 个相邻短 token
        while j < len(tokens) and j - i < 3:
            merged = "".join(tokens[i:j + 1])
            if merged in _INVOICE_HEADER_KEYWORDS:
                # 合并成功，跳过中间 token
                result.append(merged)
                i = j + 1
                break
            j += 1
        else:
            result.append(tokens[i])
            i += 1

    return result


def _is_data_value(token: str) -> bool:
    """判断 token 是否为数据值（数字、百分比等）。

    Args:
        token: 待检查的 token。

    Returns:
        是否为数据值。
    """
    token = token.strip()
    # 含括号数值（如 "37577.97(度)"）
    if re.match(r'^[\d.]+\([^)]*\)$', token):
        return True
    # 货币金额
    if re.match(r'^[¥￥][\d.,]+$', token):
        return True
    # 百分比
    if re.match(r'^[\d.]+\s*%$', token):
        return True
    # 数字
    if re.match(r'^[\d.,]+$', token):
        return True
    # 中文表头后缀接数值（如 "金额222875.57"、"税额28973.82"、"数量319620"）
    # VLM 常将表头标签和数值拼接在同一个 token 中，此处提取尾部数值部分
    if re.match(r'^[一-鿿]+[¥￥]?\d[\d.,]*$', token):
        return True
    return False


def _has_header_row(table: Tag, total_columns: int) -> bool:
    """检查表格是否已有表头行（含 <th> 元素或接近总列数的行）。

    Args:
        table: BeautifulSoup <table> Tag。
        total_columns: 表格总列数。

    Returns:
        True 表示已有表头行。
    """
    for row in table.find_all("tr"):
        th_cells = row.find_all("th")
        if th_cells:
            return True
        # 检查是否有单元格数量接近总列数的文本行（可能是已有的表头）
        td_cells = row.find_all("td")
        if len(td_cells) >= total_columns * 0.7:
            # 该行所有单元格都短（像标签），则视为表头
            texts = [c.get_text().strip() for c in td_cells]
            if all(len(t) < 20 and not _is_data_value(t) for t in texts if t):
                return True
    return False


def _insert_header_row(
    soup: BeautifulSoup, data_row: Tag, header_labels: list[str]
) -> None:
    """在数据行之前插入一个表头行（<tr><th>...</th></tr>）。

    Args:
        soup: BeautifulSoup 对象，用于创建新标签。
        data_row: 数据行 <tr> Tag，表头行将插入到此行之前。
        header_labels: 表头标签列表。
    """
    if not header_labels:
        return

    header_tr = soup.new_tag("tr")
    for label in header_labels:
        th = soup.new_tag("th")
        th.string = label
        header_tr.append(th)

    data_row.insert_before(header_tr)
    logger.debug(
        f"已插入表头行：{len(header_labels)}列，"
        f"标签={header_labels[:4]}..."
    )


def _fix_embedded_summary(
    values: list[str],
    header_labels: list[str],
) -> list[str]:
    """修正嵌入在数据行中的合计/小计数值对齐。

    当 VLM 将 "合计 ¥X ¥Y" 混在一行数据中时，
    识别 "合计" 关键词并利用表头标签的语义列位置来放置数值。

    策略：
    1. 在 header_labels 中找到 "金额" 和 "税额" 等汇总目标列的位置
    2. 如果没有精确匹配，用表头中后 50% 的列作为数值列候选
    3. 从左到右分配汇总数值到候选列

    Args:
        values: 可能存在合计相关值的列数据。
        header_labels: 表头标签列表，用于语义列匹配。

    Returns:
        修正后的列数据。
    """
    total_keywords = {"合计", "小计"}
    total_idx = -1

    for i in range(1, len(values)):
        if values[i] and values[i].strip() in total_keywords:
            total_idx = i
            break

    if total_idx < 0:
        return values

    # 查找合计后的数值
    summary_numbers = []
    for i in range(total_idx + 1, len(values)):
        if values[i] and _is_data_value(values[i]):
            summary_numbers.append(values[i])

    if not summary_numbers:
        return values

    n_nums = len(summary_numbers)
    n_cols = len(values)

    # 用表头标签的语义来确定目标列
    target_slots = _resolve_summary_target_slots(
        summary_numbers, header_labels, values, total_idx, n_cols
    )

    if target_slots is None:
        return values

    # 放置数值到目标列
    for target, num in zip(target_slots, summary_numbers):
        if 0 <= target < n_cols:
            values[target] = num

    # 合并 "合计" 标签到第一列
    if values[0]:
        values[0] += " 合计"
    else:
        values[0] = "合计"

    # 清空已被移动到目标列的数值的原位置（仅清空被移动过的数值，保留其余内容）
    for i in range(total_idx, n_cols):
        if i not in target_slots and values[i] in summary_numbers:
            values[i] = ""

    return values


def _resolve_summary_target_slots(
    summary_numbers: list[str],
    header_labels: list[str],
    values: list[str],
    total_idx: int,
    n_cols: int,
) -> Optional[list[int]]:
    """根据表头标签的语义信息确定汇总数值应放置的列位置。

    Args:
        summary_numbers: 汇总数值列表。
        header_labels: 表头标签列表。
        values: 当前列的数值列表。
        total_idx: "合计"关键词的列位置。
        n_cols: 总列数。

    Returns:
        列索引列表，或 None（无法确定）。
    """
    n_nums = len(summary_numbers)

    if len(header_labels) == n_cols:
        # 有完整的表头标签，做语义匹配
        summary_headers = {"金额", "税额", "合计金额", "价税合计"}
        candidate_cols = []
        for i, label in enumerate(header_labels):
            if label in summary_headers:
                candidate_cols.append(i)

        if len(candidate_cols) >= n_nums:
            # 取最右侧的 N 个匹配列
            return candidate_cols[-n_nums:]

    # 回退：从表头标签推断数值列 → 后 50% 的列通常是数值列
    mid = n_cols // 2
    number_candidates = list(range(mid, n_cols))

    # 排除"合计"关键词所在的列
    number_candidates = [c for c in number_candidates if c != total_idx]

    if len(number_candidates) >= n_nums:
        return number_candidates[-n_nums:]

    # 最终回退：从最右侧开始取
    return list(range(n_cols - n_nums, n_cols))


def _allocate_to_columns(
    data_tokens: list[str],
    num_columns: int,
) -> Optional[list[str]]:
    """将数据 token 分配到各列。

    优先使用智能分配：根据 token 的类型（文本/数值）推断列边界。
    退而求其次使用固定位置补空策略。

    Args:
        data_tokens: 纯数据 token 列表。
        num_columns: 目标列数。

    Returns:
        长度为 num_columns 的字符串列表，或 None。
    """
    total = len(data_tokens)

    if total == num_columns:
        return data_tokens

    if total < num_columns:
        return _pad_with_empty_smart(data_tokens, num_columns)

    # total > num_columns: 某些值被拆分为多个 token，需合并
    # 策略：从右侧保留 num_columns - 1 个，左侧多出的合并
    excess = total - num_columns
    merged_left = " ".join(data_tokens[:excess + 1])
    result = [merged_left] + data_tokens[excess + 1:]

    if len(result) == num_columns:
        return result

    return None


def _allocate_summary_row(
    header_tokens: list[str],
    data_tokens: list[str],
    total_columns: int,
    column_signature: list[str],
    reference_row_values: Optional[list[str]] = None,
) -> Optional[list[str]]:
    """按值匹配将摘要行数据分配到各列。

    摘要行如 "合计 ¥289061.35 ¥37577.97"，只有 1 个表头 + 2 个数据。
    通过比较数据值与主数据行的值来确定每列的正确位置。

    算法：
    1. 对每个数据 token，去掉 ¥ 前缀，与 reference_row_values 比较
    2. 数值相等 → 放在该列
    3. 无匹配 → 回退：从左到右分配到 number 列
    4. 表头标签放在最左侧的文本列
    5. 其余列填空

    Args:
        header_tokens: 表头标签列表（如 ["合计"]）。
        data_tokens: 纯数据 token 列表（如 ["¥289061.35", "¥37577.97"]）。
        total_columns: 表格总列数。
        column_signature: 列类型签名。
        reference_row_values: 主数据行各列的值，用于数值匹配。

    Returns:
        长度为 total_columns 的字符串列表，或 None。
    """
    result = [""] * total_columns

    number_indices = [
        i for i, t in enumerate(column_signature) if t == "number"
    ]
    text_indices = [
        i for i, t in enumerate(column_signature) if t == "text"
    ]

    if not number_indices:
        return None

    # 对每个数据 token，尝试通过数值匹配确定列位置
    assigned_indices = set()
    unmatched_tokens = []  # 收集无法匹配的 token

    for dt in data_tokens:
        # 去掉 ¥/￥ 前缀和千分位逗号，提取纯数值
        clean_value = dt.strip().lstrip("¥￥").replace(",", "")
        matched = False

        if reference_row_values:
            for ni in number_indices:
                if ni in assigned_indices:
                    continue
                ref = reference_row_values[ni].strip()
                # 主数据行的值可能也是千分位格式，统一去掉逗号比较
                ref_clean = ref.replace(",", "")
                # 也尝试小数点位数的模糊匹配（如 0.72 vs 0.716）
                if clean_value == ref_clean:
                    result[ni] = dt
                    assigned_indices.add(ni)
                    matched = True
                    break
                elif _is_fuzzy_number_match(clean_value, ref_clean):
                    result[ni] = dt
                    assigned_indices.add(ni)
                    matched = True
                    break

        if not matched:
            unmatched_tokens.append(dt)

    # 未匹配的 token：从右向左填入未占用的 number 列
    # （与 VLM 输出的数据顺序一致，右侧数值通常在右侧列）
    unassigned = [ni for ni in number_indices if ni not in assigned_indices]
    for i, dt in enumerate(reversed(unmatched_tokens)):
        idx = len(unassigned) - 1 - i
        if 0 <= idx < len(unassigned):
            result[unassigned[idx]] = dt

    # 表头标签放到最左侧的文本列
    if header_tokens and text_indices:
        result[text_indices[0]] = " ".join(header_tokens)
    elif header_tokens:
        # 没有文本列，放在第一个未被占用的 number 列
        for ni in number_indices:
            if ni not in assigned_indices:
                result[ni] = " ".join(header_tokens)
                break

    return result


def _fix_parenthetical_annotations(split_values: list[str]) -> list[str]:
    """修正括号注释的错位问题。

    当 VLM 将括号注释（如 "(度)"）错误附加到数值 token 尾部时
    （如 "37577.97(度)"），将其剥离并移动到前一个非空文本列。

    场景：中国增值税发票中，"千瓦时(度)" 的 "(度)" 常被 VLM
    拼接到税额 "37577.97" 后面，变成 "37577.97(度)"。

    Args:
        split_values: 已分配到各列的数据值列表。

    Returns:
        修正后的列表。
    """
    result = list(split_values)
    paren_pattern = re.compile(r'^([\d.]+)\(([^)]+)\)$')

    for i in range(len(result)):
        match = paren_pattern.match(result[i].strip()) if result[i] else None
        if not match:
            continue

        num_value = match.group(1)
        annotation = f"({match.group(2)})"

        # 向前查找最近的非空文本列，将注释附加到该列
        for j in range(i - 1, -1, -1):
            if result[j] and _is_text_token(result[j]):
                result[j] = f"{result[j]}{annotation}"
                result[i] = num_value
                logger.debug(
                    f"括号注释修正：将'{annotation}'从列{i}移回列{j}"
                )
                break

    return result


def _is_fuzzy_number_match(val1: str, val2: str) -> bool:
    """判断两个数值字符串是否近似相等（精度不同时容错）。

    场景：合计行数值与数据行数值同一含义但精度可能不同（如 0.72 vs 0.716）。

    Args:
        val1: 数值字符串1。
        val2: 数值字符串2。

    Returns:
        True 表示近似匹配。
    """
    try:
        n1 = float(val1)
        n2 = float(val2)
    except ValueError:
        return False

    # 两者都非零：允许 1% 误差
    if n1 != 0 and n2 != 0:
        ratio = abs(n1 - n2) / max(abs(n1), abs(n2))
        return ratio < 0.01

    # 两者都接近零
    return abs(n1 - n2) < 0.01


def _pad_with_empty_smart(
    data_tokens: list[str],
    num_columns: int,
) -> list[str]:
    """智能插入空字符串以补齐列数。

    策略：
    1. 文本 token 放左侧列，数值 token 放右侧列
    2. 空列在文本组内部插入（规格型号位置）
    3. 剩余空列放在文本和数值之间的"大间隙"处

    Args:
        data_tokens: 纯数据 token 列表。
        num_columns: 目标列数。

    Returns:
        长度为 num_columns 的字符串列表。
    """
    shortfall = num_columns - len(data_tokens)
    if shortfall <= 0:
        return data_tokens

    # 对每个 token 分类
    token_types = []
    for t in data_tokens:
        token_types.append("text" if _is_text_token(t) else "number")

    # 找出文本段的结束位置和数值段的开始位置
    last_text_idx = -1
    first_number_idx = len(data_tokens)
    for i, tt in enumerate(token_types):
        if tt == "text":
            last_text_idx = i
        elif first_number_idx == len(data_tokens):
            first_number_idx = i

    result = list(data_tokens)

    # 阶段1：在文本段内部的连续 text token 之间插入空列（规格型号位置）
    insertions = 0
    for i in range(len(token_types) - 1):
        if insertions >= shortfall:
            break
        if token_types[i] == "text" and token_types[i + 1] == "text":
            result.insert(i + 1 + insertions, "")
            insertions += 1

    # 阶段2：剩余空列插入到"文本段结束"和"数值段开始"之间（大间隙）
    remaining = shortfall - insertions
    if remaining > 0 and last_text_idx >= 0 and first_number_idx <= len(data_tokens):
        gap_pos = last_text_idx + 1 + insertions
        for _ in range(remaining):
            result.insert(gap_pos, "")
    elif remaining > 0:
        # 全部文本或全部数值：左侧补空
        for _ in range(remaining):
            result.insert(0, "")

    # 确保最终列数正确
    while len(result) < num_columns:
        result.insert(0, "")
    while len(result) > num_columns:
        result.pop()

    return result


def _is_text_token(token: str) -> bool:
    """判断 token 是否为文本类型（而非数值类型）。

    Args:
        token: 待判断的 token。

    Returns:
        True 表示文本类型。
    """
    return not _is_data_value(token)


# 合计/小计类摘要关键词（用于识别被混入数据行中的合计标签）
_SPLIT_SUMMARY_KEYWORDS = {"合计", "小计", "总计", "本页小计", "累计"}


def split_summary_from_data_cell(html: str) -> str:
    """拆分被 VLM 错误混入数据单元格中的合计/小计摘要标签。

    当 VLM 将"合计"等摘要标签与数据项拼接在同一单元格时
    （如 <td>*供电*电费 合计</td>），此函数将该单元格拆分为：
    - 原行保留纯数据标签（如 "*供电*电费"）
    - 在下方插入新的合计行（"合计" + 原行的数值列）

    同时兼容"合计"独占一个空单元格 + 其余列为空的情况
    （如 <td></td><td></td><td>合计</td><td></td>...），
    此时在合计列之前插入一行，将数值从前方数据行传递下来。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        处理后的 HTML 字符串；若无需处理则返回原字符串。
    """
    if not html or not isinstance(html, str):
        return html
    if "<table" not in html.lower():
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过合计拆分")
        return html

    modified = False
    for table in soup.find_all("table"):
        try:
            # [自定义] 仅对发票表格执行合计/小计拆分。
            # 财务报表等通用表格中的「流动资产合计」「负债合计」「所有者权益合计」
            # 等是合法的会计科目行标签，结尾虽含「合计」但不应拆分。
            if not _is_invoice_table(table):
                continue
            _split_summary_rows_in_table(soup, table)
            modified = True  # 内部无异常即认为可能已修改
        except Exception:
            logger.exception("处理表格合计拆分时出错，跳过此表格")
            continue

    if modified:
        # 仅当 HTML 确实发生变化时才返回新结果
        result = str(soup)
        if result != html:
            return result
    return html


def _next_row_contains_numeric_data(row: Tag) -> bool:
    """检查指定行是否包含数值数据（判断是否为数据行而非子表头行）。

    当行中存在 rowspan 单元格时，其下一行可能是：
    - 子表头行（如"优先股|永续债|其他"）：全部为短文本标签，不含数值
    - 数据行（如含金额/数量等）：包含数值单元格

    此函数用于在合计标签拆分时区分多行表头表格和普通数据表格，
    避免将列头中结尾为"合计"的单元格（如"所有者权益合计"）错误拆分。

    Args:
        row: <tr> Tag。

    Returns:
        True 表示该行包含数值数据（判定为数据行）。
    """
    cells = row.find_all(["td", "th"])
    if not cells:
        return False

    numeric_count = 0
    non_empty_count = 0
    for cell in cells:
        text = cell.get_text().strip()
        if not text:
            continue
        non_empty_count += 1
        if _is_data_value(text):
            numeric_count += 1

    if non_empty_count == 0:
        return False
    # 至少 20% 的非空单元格包含数值 → 判定为数据行
    return numeric_count / non_empty_count >= 0.2


def _split_summary_rows_in_table(soup: BeautifulSoup, table: Tag) -> None:
    """在单个 <table> 中查找并拆分混入数据行的合计标签。

    支持两种模式：
    1. 嵌入模式：数据行某单元格同时包含数据标签和合计关键词
       （如 "*供电*电费 合计"），拆分出一个独立的合计行。
    2. 空值模式：某行仅有一个单元格包含"合计"且其余全空，
       在前一行有数值数据时，将其转为完整的合计数值行。

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return

    total_columns = _compute_total_columns(rows)
    if total_columns < 2:
        return

    # ---- 第一遍扫描：嵌入模式 ----
    for row_idx, row in enumerate(rows):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        for cell_idx, cell in enumerate(cells):
            # 仅处理 <td> 数据单元格，跳过 <th> 表头
            if cell.name == "th":
                continue
            cell_text = cell.get_text().strip()
            if not cell_text:
                continue

            matched_keyword = _find_trailing_summary_keyword(cell_text)
            if matched_keyword is None:
                continue

            # 剥离关键词后的剩余文本
            data_part = _strip_summary_keyword(cell_text, matched_keyword)
            if not data_part:
                # 整个单元格就是"合计"，不需要拆分
                continue

            # 嵌入模式：更新原单元格为纯数据标签，然后插入合计行

            # 检测数据行是否存在 rowspan > 1 的单元格
            # 若存在，需特殊处理以避免 rowspan 溢出到新插入的合计行
            has_rowspan = any(
                int(c.get("rowspan", 1)) > 1 for c in cells
            )

            if has_rowspan:
                # 【自定义】检查下一行是否为数据行（包含数值）。
                # 多行表头表格（如所有者权益变动表）中，rowspan 行的下一行
                # 是子表头行（如"优先股|永续债|其他"），不含数值，不应拆分。
                # 仅当下一行确实包含数值数据时才执行 rowspan 模式拆分。
                next_row = row.find_next_sibling("tr")
                if next_row and _next_row_contains_numeric_data(next_row):
                    cell.string = data_part
                    _handle_summary_split_with_rowspan(
                        soup, row, cells, matched_keyword, total_columns
                    )
                    logger.debug(
                        f"合计标签拆分（嵌入模式 +rowspan）："
                        f"行{row_idx}列{cell_idx}，"
                        f"关键词={matched_keyword}，数据部分={data_part[:30]}"
                    )
                    return  # 每个表格只处理一次
                else:
                    logger.debug(
                        f"合计标签拆分跳过（多行表头结构，下一行不含数值）："
                        f"行{row_idx}列{cell_idx}，"
                        f"关键词={matched_keyword}，数据部分={data_part[:30]}"
                    )
            else:
                cell.string = data_part
                _insert_summary_row_after(
                    soup, row, matched_keyword, cells, total_columns
                )
                logger.debug(
                    f"合计标签拆分（嵌入模式）："
                    f"行{row_idx}列{cell_idx}，"
                    f"关键词={matched_keyword}，数据部分={data_part[:30]}"
                )
                return  # 每个表格只处理一次

    # ---- 第二遍扫描：空值模式 ----
    # 某行中"合计"独占一个单元格且同行其余全空
    for row_idx in range(len(rows)):
        row = rows[row_idx]
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        # 检查是否为"合计独占空行"模式
        summary_cell_idx = _find_isolated_summary_cell(cells)
        if summary_cell_idx is None:
            continue

        # 合计已在第一列（标准位置），跳过空值模式
        # VLM 多行格式输出的干净合计行（合计在列0），结构已正确，
        # 不需要重组。合计行缺少的 ¥ 值由后续 fix_summary_row_yen_position 负责填充。
        # 仅当 summary_cell_idx != 0 时才需要空值模式的列位置调整
        # （如 OCR 重建从 VLM 拼接格中拆分出的合计）。
        if summary_cell_idx == 0:
            continue

        # 查找前一行是否有数值数据
        if row_idx == 0:
            continue
        prev_row = rows[row_idx - 1]
        prev_cells = prev_row.find_all(["td", "th"])
        prev_number_values = _extract_number_cells(prev_cells)

        if not prev_number_values:
            continue

        # 空值模式：收集前一行所有数值，按列顺序填入合计行
        # 策略：使用前一行各列的数值特征来确定哪些列应填入数值
        prev_cell_texts = [_get_cell_text(prev_cells, i) for i in range(len(prev_cells))]
        prev_number_cols = [
            i for i, t in enumerate(prev_cell_texts)
            if t.strip() and _is_data_value(t.strip())
        ]
        prev_text_cols = [
            i for i, t in enumerate(prev_cell_texts)
            if t.strip() and not _is_data_value(t.strip())
        ]

        if not prev_number_cols:
            continue

        # 保存关键信息（在清空单元格之前）
        saved_label = cells[summary_cell_idx].get_text().strip() if summary_cell_idx < len(cells) else "合计"

        # 先将合计行所有单元格清空
        for col_idx in range(len(cells)):
            cells[col_idx].string = ""

        # 在第一个文本列放置"合计"标签（避免覆盖数值列）
        label_col = prev_text_cols[0] if prev_text_cols else 0
        cells[label_col].string = saved_label

        # 将前一行数值列的每个值填入合计行的对应列
        filled_count = 0
        for ncol in prev_number_cols:
            if ncol >= len(cells):
                continue
            if ncol == label_col:
                # 标签已占据此列，查找下一个空数值列
                placed = False
                for alt_col in prev_number_cols:
                    if alt_col >= len(cells):
                        continue
                    cell_text = cells[alt_col].get_text().strip()
                    if not cell_text:
                        cells[alt_col].string = prev_cell_texts[ncol].strip()
                        filled_count += 1
                        placed = True
                        break
                if not placed:
                    # 最终回退：填入任何一个空单元格
                    for alt_col in range(len(cells)):
                        if alt_col == label_col:
                            continue
                        if not cells[alt_col].get_text().strip():
                            cells[alt_col].string = prev_cell_texts[ncol].strip()
                            filled_count += 1
                            break
            else:
                cells[ncol].string = prev_cell_texts[ncol].strip()
                filled_count += 1

        logger.debug(
            f"合计行填充（空值模式）：行{row_idx}列{summary_cell_idx}，"
            f"从前行填充了{filled_count}个数值"
        )
        return


def _find_trailing_summary_keyword(text: str) -> Optional[str]:
    """检查文本末尾是否包含合计/小计类摘要关键词。

    支持 VLM 输出中关键词内嵌空格的变体（如 "合 计" 等同于 "合计"）。

    Args:
        text: 单元格文本。

    Returns:
        匹配到的关键词字符串（规范形式，无内嵌空格），或 None。
    """
    # 规范化：去除文本中所有空白字符以兼容 VLM 内嵌空格变体
    collapsed = re.sub(r'\s+', '', text)
    for kw in sorted(_SPLIT_SUMMARY_KEYWORDS, key=len, reverse=True):
        if collapsed.endswith(kw):
            return kw
    return None


def _strip_summary_keyword(text: str, keyword: str) -> str:
    """从文本中移除末尾的摘要关键词。

    支持 VLM 输出中关键词内嵌空格的变体（如 "合 计" 等同于 "合计"）。

    Args:
        text: 原始文本。
        keyword: 要移除的关键词（规范形式，无内嵌空格）。

    Returns:
        剥离后的文本。
    """
    # 构建正则：关键词各字符之间允许零或多个空白（如 "合 计"、"合  计"）
    spaced_pattern = r'\s*'.join(list(keyword)) + r'\s*$'
    m = re.search(spaced_pattern, text)
    if m:
        return text[:m.start()].rstrip()
    return text


def _insert_summary_row_after(
    soup: BeautifulSoup,
    data_row: Tag,
    summary_label: str,
    data_cells: list[Tag],
    total_columns: int,
) -> None:
    """在数据行之后插入一个合计摘要行。

    合计行的结构：
    - 第一列（或最左侧文本列）放置摘要标签（如"合计"）
    - 数值列复制数据行中的数值（如金额、税额）
    - 其余列留空

    Args:
        soup: BeautifulSoup 对象。
        data_row: 当前数据行 <tr>。
        summary_label: 摘要标签文本（如"合计"）。
        data_cells: 数据行的单元格列表。
        total_columns: 表格总列数。
    """
    summary_tr = soup.new_tag("tr")

    # 提取数据行各列的文本和数值特征
    cell_texts = [_get_cell_text(data_cells, i) for i in range(len(data_cells))]

    for col_idx in range(total_columns):
        td = soup.new_tag("td")
        if col_idx < len(cell_texts):
            cell_val = cell_texts[col_idx].strip() if cell_texts[col_idx] else ""
        else:
            cell_val = ""

        if col_idx == 0:
            # 第一列放置摘要标签
            td.string = summary_label
        elif "¥" in cell_val or "￥" in cell_val:
            # 从单元格中提取所有 ¥/￥ 前缀的金额值
            yen_values = re.findall(r'[¥￥][\d.,]+', cell_val)
            td.string = " ".join(yen_values) if yen_values else ""
            # 注意：此处不清理源数据行中的 ¥ 值。
            # ¥ 值的完整移动（从数据行提取 → 按 colspan 展开列索引
            # 放置到合计行）由下游 extract_column_header_prefixes() 统一处理，
            # 该函数拥有正确的 colspan 展开逻辑。
        else:
            # 其余列留空
            td.string = ""
        summary_tr.append(td)

    data_row.insert_after(summary_tr)


def _handle_summary_split_with_rowspan(
    soup: BeautifulSoup,
    data_row: Tag,
    data_cells: list[Tag],
    summary_label: str,
    total_columns: int,
) -> None:
    """处理带 rowspan 的合计行拆分。

    当数据行包含 rowspan>1 的单元格时（如增值税发票中"货物名称"、
    "规格型号"等列跨两行，"金额"和"税额"列不跨行），
    分析列结构后将表头标签提取为独立的 <th> 行，将 ¥ 值合并到数据行。

    策略：
    1. 分析各列 rowspan 状态，找出非 rowspan 列（¥ 值所在列）
    2. 减少 rowspan
    3. 提取表头标签 + 清理数据单元格 + 插入 <th> 表头行
    4. 合并 ¥ 行值到数据行 + 删除 ¥ 行
    5. 创建合计行

    Args:
        soup: BeautifulSoup 对象。
        data_row: 当前数据行 <tr>。
        data_cells: 数据行的单元格列表。
        summary_label: 摘要标签（如"合计"）。
        total_columns: 表格总列数。
    """
    # 1. 分析各列的 rowspan 状态
    non_rowspan_cols: list[tuple[int, int]] = []  # [(cell_index, effective_start_col)]
    current_col = 0
    for i, cell in enumerate(data_cells):
        colspan = int(cell.get("colspan", 1))
        rowspan = int(cell.get("rowspan", 1))
        if rowspan <= 1:
            non_rowspan_cols.append((i, current_col))
        current_col += colspan

    # 2. 减少 rowspan
    for cell in data_cells:
        rs = int(cell.get("rowspan", 1))
        if rs > 1:
            if rs == 2:
                del cell["rowspan"]
            else:
                cell["rowspan"] = str(rs - 1)

    # 3. 提取表头标签 + 清理数据 + 插入 <th> 表头行
    #    VLM 输出如 "单位kw.h"、"金额222875.57" 是表头+值拼接，
    #    利用 _strip_header_prefix 和 _INVOICE_HEADER_KEYWORDS 做分离
    header_labels: list[tuple[int, str, int]] = []  # [(col_idx, label, colspan)]
    for i, cell in enumerate(data_cells):
        text = cell.get_text().strip()
        colspan = int(cell.get("colspan", 1))
        if not text:
            continue

        # 情况 A：纯表头关键词（如"规格型号"）→ 数据行该列清空
        if text in _INVOICE_HEADER_KEYWORDS:
            header_labels.append((i, text, colspan))
            cell.string = ""
        # 情况 B：表头+值拼接（如"单位kw.h"）→ 分离后保留纯数据值
        elif (stripped := _strip_header_prefix(text)):
            header_labels.append((i, stripped["header"], colspan))
            cell.string = stripped["data"]
        # 情况 C：无法拆分 → 保持原样

    if header_labels:
        header_tr = soup.new_tag("tr")
        for _col_idx, label, cs in header_labels:
            th = soup.new_tag("th")
            th.string = label
            if cs > 1:
                th["colspan"] = str(cs)
            header_tr.append(th)
        data_row.insert_before(header_tr)
        logger.debug(
            f"表头行提取：{len(header_labels)} 个标签"
        )

    # 4. 合并 ¥ 行值到数据行 + 删除 ¥ 行
    #    ¥ 行原有的 ¥222875.57 / ¥28973.82 合并到数据行对应列
    #    消除数据行中 "金额222875.57" 和 ¥ 行 "¥222875.57" 的重复
    next_row = data_row.find_next_sibling("tr")
    next_cells = next_row.find_all(["td", "th"]) if next_row else []

    copied_count = 0
    for idx, (cell_idx, _eff_col) in enumerate(non_rowspan_cols):
        if idx < len(next_cells) and cell_idx < len(data_cells):
            # 仅当 ¥ 行对应格非空时才覆盖，避免用空值清空数据行已识别的值
            # （如「数 量\n1622」拆出的 1622 被 ¥ 行为空的对应格覆盖为空）
            val = next_cells[idx].get_text().strip()
            if val:
                data_cells[cell_idx].string = val
                copied_count += 1

    if next_row:
        # 验证所有非空 next_cells 值都已被复制，未复制的记录日志
        total_next_vals = len([c for c in next_cells if c.get_text().strip()])
        if copied_count < total_next_vals:
            logger.warning(
                f"¥行删除前：{total_next_vals} 个非空值中仅复制了 {copied_count} 个，"
                f"可能存在内容丢失"
            )
        next_row.decompose()

    # 5. 创建合计行——从合并后的数据行取值
    summary_tr = soup.new_tag("tr")
    non_rowspan_col_set = {col for _, col in non_rowspan_cols}

    for col_idx in range(total_columns):
        td = soup.new_tag("td")
        if col_idx == 0:
            td.string = summary_label
        elif col_idx in non_rowspan_col_set:
            match_idx = None
            for j, (_, eff_col) in enumerate(non_rowspan_cols):
                if eff_col == col_idx:
                    match_idx = j
                    break
            if match_idx is not None and match_idx < len(non_rowspan_cols):
                cell_idx = non_rowspan_cols[match_idx][0]
                if cell_idx < len(data_cells):
                    val = data_cells[cell_idx].get_text().strip()
                    td.string = val if _is_data_value(val) else ""
            else:
                td.string = ""
        else:
            td.string = ""
        summary_tr.append(td)

    data_row.insert_after(summary_tr)

    logger.debug(
        f"合计标签拆分（rowspan模式）：非rowspan列={non_rowspan_cols}，"
        f"¥行已合并删除"
    )


def _find_isolated_summary_cell(cells: list[Tag]) -> Optional[int]:
    """查找包含摘要关键词的孤立单元格（同行为空行模式）。

    条件：
    - 某单元格文本是纯摘要关键词（如 "合计"）
    - 同行其余所有单元格的文本要么为空，要么不含数据值

    Args:
        cells: 一行中的所有 <td>/<th> 元素。

    Returns:
        摘要单元格的列索引，或 None。
    """
    summary_idx = None
    for i, cell in enumerate(cells):
        text = cell.get_text().strip()
        if text in _SPLIT_SUMMARY_KEYWORDS:
            if summary_idx is not None:
                return None  # 多个摘要关键词，不符合预期
            summary_idx = i
        elif text and _is_data_value(text):
            return None  # 同行有其他数据，不是孤立摘要行

    return summary_idx


def _extract_number_cells(cells: list[Tag]) -> list[tuple[int, str]]:
    """提取一行中所有数值单元格的列索引和值。

    Args:
        cells: 一行的 <td>/<th> 列表。

    Returns:
        [(列索引, 单元格文本)] 列表，仅包含数值类型单元格。
    """
    result = []
    for i, cell in enumerate(cells):
        text = cell.get_text().strip()
        if text and _is_data_value(text):
            result.append((i, text))
    return result


def _get_cell_text(cells: list[Tag], idx: int) -> str:
    """安全获取单元格文本（处理索引越界）。

    Args:
        cells: 单元格列表。
        idx: 列索引。

    Returns:
        单元格文本，越界返回空字符串。
    """
    if 0 <= idx < len(cells):
        return cells[idx].get_text()
    return ""


def normalize_table_colspan(html: str) -> str:
    """规范化表格各行的 colspan，使所有行的列宽总和一致。

    VLM 生成的表格 HTML 中，不同行的 colspan 总和可能不一致
    （如增值税发票中表头行定义 8 列，但购买方/销售方行仅 6 列），
    导致渲染时表格右侧缩进错位。

    算法：
    1. 遍历所有行，找到最大的 colspan 总和作为基准列数
    2. 对总和不足的行，将差额按比例分配到已有 colspan>1 的单元格
    3. 若该行无 colspan 单元格，则扩展最后一个单元格的 colspan

    Args:
        html: 表格 HTML 字符串。

    Returns:
        规范化后的 HTML 字符串；若无需修改则返回原字符串。
    """
    if not html or not isinstance(html, str):
        return html
    if "<table" not in html.lower():
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过 colspan 规范化")
        return html

    modified = False
    for table in soup.find_all("table"):
        try:
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # 以最大 colspan 总和为基准列数
            ref_cols = 0
            for row in rows:
                total = sum(
                    int(c.get("colspan", 1))
                    for c in row.find_all(["td", "th"])
                )
                ref_cols = max(ref_cols, total)

            if ref_cols < 2:
                continue

            # 逐行检查并修正
            for row in rows:
                cells = row.find_all(["td", "th"])
                current_sum = sum(
                    int(c.get("colspan", 1)) for c in cells
                )

                if current_sum == ref_cols:
                    continue

                diff = ref_cols - current_sum
                if diff <= 0:
                    continue

                # 找到具有 colspan>1 的单元格
                colspan_cells = [
                    (i, c) for i, c in enumerate(cells)
                    if int(c.get("colspan", 1)) > 1
                ]

                if colspan_cells:
                    # 按原有 colspan 比例分配差额
                    total_span = sum(
                        int(c.get("colspan", 1)) for _, c in colspan_cells
                    )
                    allocated = 0
                    for idx, (_, cell) in enumerate(colspan_cells):
                        if idx == len(colspan_cells) - 1:
                            extra = diff - allocated
                        else:
                            extra = max(
                                1,
                                int(diff * int(cell.get("colspan", 1)) / total_span),
                            )
                        cell["colspan"] = str(
                            int(cell.get("colspan", 1)) + extra
                        )
                        allocated += extra
                    modified = True
                elif cells:
                    # 无 colspan 单元格：扩展最后一个单元格
                    last = cells[-1]
                    last["colspan"] = str(
                        int(last.get("colspan", 1)) + diff
                    )
                    modified = True

        except Exception:
            logger.exception(
                "处理表格 colspan 规范化时出错，跳过此表格"
            )
            continue

    if modified:
        return str(soup)
    return html


def _format_summary_row_colspan(
    soup: BeautifulSoup, table: Tag
) -> None:
    """将合计/小计行的连续空单元格合并为单个 colspan 单元格。

    将：
        <tr><td>合计</td><td></td><td></td><td></td><td></td><td>¥X</td>...
    转换为：
        <tr><td colspan="5">合计</td><td>¥X</td>...

    算法：
    1. 识别包含摘要关键词的行（合计/小计/总计）
    2. 找到该行中"从摘要标签到第一个数据值之前"的所有连续空单元格
    3. 将摘要标签和这些空单元格合并为一个 colspan 单元格

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
    """
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        # 查找包含摘要标签的单元格位置
        summary_col_idx = None
        for i, cell in enumerate(cells):
            text = cell.get_text().strip()
            if text in _SPLIT_SUMMARY_KEYWORDS:
                summary_col_idx = i
                break
        if summary_col_idx is None:
            continue

        # 统计摘要标签所在单元格之后、第一个非空数据单元格之前
        # 有多少个连续的空单元格
        first_data_idx = None
        for i in range(summary_col_idx + 1, len(cells)):
            text = cells[i].get_text().strip()
            if text:
                first_data_idx = i
                break

        if first_data_idx is None or first_data_idx <= summary_col_idx + 1:
            # 没有连续空单元格需要合并
            continue

        # 计算合并跨度：从摘要标签列到第一个数据列之前
        merge_span = first_data_idx - summary_col_idx

        if merge_span < 2:
            continue

        # 将摘要标签单元格加上 colspan
        cells[summary_col_idx]["colspan"] = str(merge_span)

        # 删除被合并的空单元格
        for i in range(summary_col_idx + 1, first_data_idx):
            cells[i].decompose()

        logger.debug(
            f"合计行 colspan 格式化：合并{merge_span}列，"
            f"标签={cells[summary_col_idx].get_text().strip()}"
        )
        return  # 每表只处理一个摘要行


def _infer_missing_values_in_table(
    soup: BeautifulSoup, table: Tag
) -> None:
    """推断并填充表格中缺失的数值单元格。

    当前支持：
    - 税率/征收率推断：当 金额 和 税额 均有值但 税率 为空时，
      通过 税额 ÷ 金额 × 100 计算出税率（如 ¥80.53 ÷ ¥619.47 = 13%）。

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
    """
    import os

    rows = table.find_all("tr")
    if len(rows) < 2:
        return

    # 第一步：找到表头行，确定金额、税额、税率列的索引
    header_indices: dict[str, int] = {}
    for row in rows:
        th_cells = row.find_all("th")
        if not th_cells:
            # 检查是否为类表头行（所有单元格都是短文本且不含发票特有长文本）
            td_cells = row.find_all("td")
            texts = [c.get_text().strip() for c in td_cells]
            if texts and all(
                len(t) < 15 and not _is_data_value(t)
                for t in texts if t
            ):
                # 排除购买方/销售方信息行（含"纳税人识别号"等长文本）
                if not any("纳税人识别号" in t for t in texts):
                    th_cells = td_cells
        if not th_cells:
            continue

        for i, cell in enumerate(th_cells):
            text = cell.get_text().strip()
            if text in ("金额",):
                header_indices["amount"] = i
            elif text in ("税额",):
                header_indices["tax"] = i
            elif text in ("税率", "税率/征收率",):
                header_indices["rate"] = i

        # 找到所需的所有列索引后退出
        if len(header_indices) >= 2:
            break

    if "rate" not in header_indices:
        return
    rate_col = header_indices["rate"]

    # 第二步：遍历数据行，尝试推断缺失的税率
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) <= max(header_indices.values()):
            continue

        rate_text = _get_cell_text(cells, rate_col).strip()
        if rate_text:
            continue  # 税率已存在，跳过

        # 获取金额和税额值
        amount_text = ""
        tax_text = ""
        if "amount" in header_indices:
            amount_text = _get_cell_text(
                cells, header_indices["amount"]
            ).strip()
        if "tax" in header_indices:
            tax_text = _get_cell_text(
                cells, header_indices["tax"]
            ).strip()

        # 尝试提取数值
        try:
            amount_val = float(
                amount_text.lstrip("¥￥").replace(",", "")
            )
            tax_val = float(
                tax_text.lstrip("¥￥").replace(",", "")
            )
        except (ValueError, AttributeError):
            continue

        if amount_val <= 0:
            continue

        # 计算税率并填充
        # [自定义] 环境变量 MINERU_INFER_MISSING_TABLE_VALUES 控制是否启用推断填充
        # 默认关闭——推断值不是识别结果，违反"输出不多不少"原则
        if not os.getenv("MINERU_INFER_MISSING_TABLE_VALUES", "").lower() in ("1", "true", "yes"):
            continue
        computed_rate = round(tax_val / amount_val * 100)
        # 仅当税率在合理范围内（2~20%）才填充
        if 2 <= computed_rate <= 20:
            rate_str = f"{computed_rate}%"
            if rate_col < len(cells):
                cells[rate_col].string = rate_str
                logger.debug(
                    f"税率推断：税额{tax_val}÷金额{amount_val}"
                    f"={computed_rate}%，已填充"
                )
        else:
            logger.debug(
                f"税率推断跳过：计算值{computed_rate}%超出合理范围"
            )


# -- 购买方/销售方信息行内多行拆分 --

# 信息单元格中可识别为行分隔点的字段标签正则
# 在"名称:"之后出现的这些标签前插入 <br/> 实现多行拆分
# (统一社会信用代码/)?纳税人识别号 兼容有无"统一社会信用代码/"前缀的两种情况
# (纳税人)?识别号: 兼容 VLM 将"纳税人"遗漏的缩写情况
# (地)?址、电话: 兼容 VLM 将"地"遗漏的缩写情况
_INFO_LINE_BREAK_RE = re.compile(
    r'(?<=.)(?:(统一社会信用代码/)?(纳税人)?识别号:|(地)?址、电话:|开户行及账号:)'
)


def split_info_cell_multiline(html: str) -> str:
    """将发票购买方/销售方信息单元格在字段边界处拆分为多行。

    检测被拼接在一行的形式如：
        "名称:xxx纳税人识别号:yyy地址、电话:zzz开户行及账号:www"
    在各字段标签前插入 <br/> 拆分为：
        名称:xxx
        纳税人识别号:yyy
        地址、电话:zzz
        开户行及账号:www

    兼容两种税号标签格式：
    - 统一社会信用代码/纳税人识别号:（含前缀）
    - 纳税人识别号:（无前缀）

    使用 re.sub 在各字段标签前插入 <br/>，完整保留所有字段内容。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        处理后的 HTML 字符串；若无匹配则返回原始 HTML。
    """
    if not html or not isinstance(html, str):
        return html
    if "<table" not in html.lower():
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析 HTML 失败，跳过信息多行拆分")
        return html

    modified = False

    for td in soup.find_all("td"):
        text = td.get_text().strip()
        if not text:
            continue

        # 跳过已包含 <br/> 的单元格（已拆分过）
        if td.find("br"):
            continue

        # 必须包含购买方/销售方信息标签才可能是发票信息单元格
        # 兼容 VLM 输出缩写的 "称:"（缺"名"字）的情况
        if not (text.startswith("名称:") or text.startswith("称:")):
            continue

        # 在字段标签前插入 <br/> 实现分行的同时保留所有字段内容
        new_text = _INFO_LINE_BREAK_RE.sub(r'<br/>\g<0>', text)
        if new_text == text:
            # 没有匹配到任何可分行的字段标签
            continue

        td.clear()
        # 将 <br/> 替换为实际的 BeautifulSoup <br> 标签
        parts = new_text.split("<br/>")
        for i, part in enumerate(parts):
            if i > 0:
                td.append(soup.new_tag("br"))
            td.append(NavigableString(part))

        modified = True
        logger.info(
            f"购买方/销售方信息多行拆分：{len(parts)} 个字段"
        )

    return str(soup) if modified else html


def _has_significant_rowspan(html: str) -> bool:
    """检查表格是否使用多层 rowspan 结构（多行表头表格等）。

    当表格存在 rowspan > 1 的单元格时，各行 colspan 总和自然不同
    （rowspan 覆盖的列不计入后续行），此时不应执行 colspan 规范化，
    否则会将子表头行的 colspan 值扩大到无意义的范围。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        True 表示存在 rowspan > 1 的单元格。
    """
    if not html or not isinstance(html, str):
        return False
    if "<table" not in html.lower():
        return False
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    for table in soup.find_all("table"):
        if table.find(attrs={"rowspan": True}):
            return True
    return False


# 增值税发票货物区「名称」列标签（专用/普通发票共用的首列表头）。
_VAT_INVOICE_NAME_LABELS = ("货物或应税劳务、服务名称", "项目名称")

# 增值税发票货物区 8 列签名中除名称列外的 7 列，用于完整签名校验。
_VAT_INVOICE_COLUMN_SIGNATURE = ("规格型号", "单位", "数量", "单价", "金额", "税率", "税额")

# 非货物区行的行级标签（收拢列数时应保留其 colspan，不压缩）。
_VAT_INVOICE_ROW_LABELS = ("价税合计", "购买方", "销售方", "密码区", "备注")


def _is_vat_invoice_row_label(text: str) -> bool:
    """判断单元格文本是否为非货物区行的行级标签。

    非货物区行（购买方/销售方/价税合计）中的窄标签列（如「购买方」「价税合计
    (大写)」）即使 colspan>1 也不应收拢，否则会破坏标签列的宽度。标签通常短小
    且命中 _VAT_INVOICE_ROW_LABELS 或以其中某标签开头（兼容「价税合计(大写)」）。

    Args:
        text: 单元格文本（未 strip）。

    Returns:
        True 表示该文本为行级标签。
    """
    t = "".join(text.split())
    if not t:
        return False
    return any(t == k or t.startswith(k) for k in _VAT_INVOICE_ROW_LABELS)


def _shrink_row_to_cols(cells: list[Tag], target_cols: int) -> None:
    """将非货物区行收拢到 target_cols，每轮对每个非标签宽单元格各减 1 列。

    增值税发票中购买方/销售方/价税合计行与货物区共享同一总列数，但 VLM 对这些
    行的宽内容块过分割（信息 5 列、密码/备注内容 3 列、大写金额 8 列）。本函数
    每轮对每个 colspan≥2 且非行级标签的单元格各减 1 列，直至总和 ≤ target_cols，
    使冗余列被各宽内容块均匀吸收；行级标签列（如「价税合计(大写)」colspan=2）
    保持不变。

    Args:
        cells: 一行中的单元格列表。
        target_cols: 目标列数。
    """
    while True:
        total = sum(int(c.get("colspan", 1)) for c in cells)
        if total <= target_cols:
            return
        shrinkable = [
            c for c in cells
            if int(c.get("colspan", 1)) >= 2
            and not _is_vat_invoice_row_label(c.get_text())
        ]
        if not shrinkable:
            # 无标签外可缩单元格，避免死循环，保持原样
            return
        # 若全部各减 1 会低于 target，则只对前 (total-target) 个宽单元格减 1
        to_shrink = shrinkable
        if total - len(shrinkable) < target_cols:
            to_shrink = shrinkable[: total - target_cols]
        for c in to_shrink:
            c["colspan"] = str(int(c.get("colspan", 1)) - 1)


def _normalize_vat_invoice_columns(soup: BeautifulSoup, table: Tag) -> None:
    """将 VLM 过分割为 10 列的增值税发票货物区收拢为 8 列。

    必然正确条件：表格为发票样式，且存在一个 TH 表头行，其首列文本 ∈
    {货物或应税劳务、服务名称, 项目名称} 且 colspan == 2，「单价」列
    colspan == 2，且该行完整包含 8 列关键词签名（名称|规格型号|单位|数量|
    单价|金额|税率|税额）。此时 VLM 把「名称」「单价」两个单列宽列误判为
    colspan=2，产出 10 列，本函数收拢为 8 列。

    变换：
    1. 货物区行（名称列与单价列位置均有 colspan≥2 的单元格）：
       把名称列、单价列 colspan 2→1。
    2. 其余行（购买方/销售方/价税合计）：把 colspan 总和收拢到目标列数，
       均匀缩减宽内容块。

    匹配失败（非 8 列签名或名称/单价 colspan 非 2）时保持原样。

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return

    # 1. 定位货物区表头行（TH 行），校验 8 列签名 + 名称/单价过分割
    header_row = None
    unit_price_idx = -1
    name_start_col = 0
    unit_price_start_col = 0
    header_total_cols = 0
    for row in rows:
        th_cells = row.find_all("th")
        if not th_cells:
            continue
        texts = [c.get_text().strip() for c in th_cells]
        # 名称列必须为首列，且 colspan == 2
        if not texts or texts[0] not in _VAT_INVOICE_NAME_LABELS:
            continue
        if int(th_cells[0].get("colspan", 1)) != 2:
            continue
        # 单价列 colspan == 2
        unit_price_idx = next(
            (i for i, t in enumerate(texts) if t == "单价"), -1
        )
        if unit_price_idx < 0 or int(th_cells[unit_price_idx].get("colspan", 1)) != 2:
            continue
        # 完整 8 列签名（除名称外其余 7 列必须齐全）
        if not set(_VAT_INVOICE_COLUMN_SIGNATURE).issubset(set(texts)):
            continue
        header_row = row
        # 计算名称/单价列起始列号与表头总列数
        col = 0
        for i, th in enumerate(th_cells):
            if i == 0:
                name_start_col = col
            if i == unit_price_idx:
                unit_price_start_col = col
            col += int(th.get("colspan", 1))
        header_total_cols = col
        break

    if header_row is None:
        return

    # 收拢后目标列数 = 表头总列数 -（名称多余列 + 单价多余列）
    name_cell = header_row.find_all("th")[0]
    unit_price_cell = header_row.find_all("th")[unit_price_idx]
    collapse_amount = (int(name_cell.get("colspan", 1)) - 1) + (
        int(unit_price_cell.get("colspan", 1)) - 1
    )
    target_cols = header_total_cols - collapse_amount

    modified = False
    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        # 计算每个单元格的起始列号与 colspan
        spans = []
        col = 0
        for c in cells:
            cs = int(c.get("colspan", 1))
            spans.append((c, col, cs))
            col += cs
        # 判断是否为货物区行：名称列与单价列位置均有 colspan≥2 的单元格
        name_cell_hit = None
        unit_price_cell_hit = None
        for c, s, cs in spans:
            if name_cell_hit is None and s == name_start_col and cs >= 2:
                name_cell_hit = c
            if unit_price_cell_hit is None and s == unit_price_start_col and cs >= 2:
                unit_price_cell_hit = c
        if name_cell_hit is not None and unit_price_cell_hit is not None:
            # 货物区行：名称/单价 colspan 收拢为 1
            if int(name_cell_hit.get("colspan", 1)) != 1:
                name_cell_hit["colspan"] = "1"
                modified = True
            if int(unit_price_cell_hit.get("colspan", 1)) != 1:
                unit_price_cell_hit["colspan"] = "1"
                modified = True
        elif col > target_cols:
            # 非货物区行：收拢到目标列数
            _shrink_row_to_cols(cells, target_cols)
            modified = True

    if modified:
        logger.info(
            f"增值税发票 8 列归一化：表头 {header_total_cols} 列收拢为 "
            f"{target_cols} 列（名称/单价 colspan 2→1）"
        )


def normalize_invoice_table(html: str) -> str:
    """发票表格专用规范化入口。

    检测表格是否为增值税发票样式，若是则依次执行：
    1. colspan 规范化（对齐各行列数）
    2. 合计行 colspan 格式化（合并连续空单元格）
    3. 缺失数值推断（如税率）

    当未检测到发票特征时，仅执行 colspan 规范化（通用操作）。

    发票检测依据：
    - 表头包含增值税发票特征关键词（项目名称、金额、税额等）
    - 表格内容包含价税合计、购买方/销售方等发票要素

    Args:
        html: 表格 HTML 字符串。

    Returns:
        规范化后的 HTML 字符串。
    """
    if not html or not isinstance(html, str):
        return html
    if "<table" not in html.lower():
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning(
            "BeautifulSoup 解析表格 HTML 失败，跳过发票规范化"
        )
        return html

    # 先执行通用的 colspan 规范化（跳过多行表头表格，其 rowspan 导致各行自然不同）
    html = (
        normalize_table_colspan(html)
        if _has_colspan_mismatch(html) and not _has_significant_rowspan(html)
        else html
    )

    # 重新解析（colspan 规范化可能修改了 HTML）
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return html

    for table in soup.find_all("table"):
        try:
            if not _is_invoice_table(table):
                continue

            logger.debug("检测到发票表格，执行发票专用规范化")
            # 8 列签名归一化（名称/单价 colspan 2→1）必须先于合计行格式化，
            # 否则收拢改变了列索引后，合计行 ¥ 对齐依赖的列位置会错位。
            _normalize_vat_invoice_columns(soup, table)
            # 合计行格式化
            _format_summary_row_colspan(soup, table)
            # 缺失值推断
            _infer_missing_values_in_table(soup, table)

        except Exception:
            logger.exception(
                "处理发票表格规范化时出错，跳过此表格"
            )
            continue

    return str(soup)


def fix_summary_row_yen_position(html: str) -> str:
    """修正所有发票表格中合计行的 ¥/￥ 值列位置。

    VLM 输出或 normalize_invoice_table 处理后，合计行中的 ¥/￥ 值
    可能被放在错误的展开列位置。此函数用 TH 行的 colspan 结构
    重建合计行，将 ¥ 值按顺序对齐到“金额”和“税额”列。

    此函数应在 normalize_invoice_table 之后调用，
    因为 normalize_invoice_table 会调整 colspan 结构。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        处理后的 HTML 字符串。
    """
    if not html or not isinstance(html, str):
        return html
    if "<table" not in html.lower():
        return html

    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过 ¥ 位置修正")
        return html

    for table in soup.find_all("table"):
        try:
            _fix_summary_row_yen_for_th_table(table, soup)
        except Exception:
            logger.exception("fix_summary_row_yen_position 处理单个表格时出错，跳过")
            continue

    return str(soup)


def _fix_summary_row_yen_for_th_table(
    table: Tag,
    soup: BeautifulSoup,
) -> None:
    """修正已有 <th> 行的发票表格中合计行的 ¥ 值列位置。

    VLM 直接输出的发票表格可能已有完整的 <th> 表头行，
    但合计行中的 ¥ 值可能被放在错误的展开列位置
    （如 colspan=5 的"合计"后 ¥ 值堆在隨后的物理列，而非"金额"列）。

    此函數用 TH 行的 colspan 结构重建合计行，
    将 ¥ 值按顺序对齐到"金额"和"税额"列。

    Args:
        table: BeautifulSoup 的 <table> 标签。
        soup: BeautifulSoup 对象。
    """
    rows = table.find_all("tr")

    # 1. 找到 TH 行并构建展开列标签
    header_tr = None
    for row in rows:
        if row.find("th"):
            header_tr = row
            break
    if header_tr is None:
        return

    th_cells = header_tr.find_all("th")
    if len(th_cells) < 3:
        return

    # 构建展开后的列标签列表
    expanded_headers: list[str] = []
    for th in th_cells:
        colspan = int(th.get("colspan", 1))
        label = th.get_text().strip()
        for _ in range(colspan):
            expanded_headers.append(label)

    # 找到"金额"和"税额"列的展开索引
    amount_cols = [i for i, h in enumerate(expanded_headers) if h == "金额"]
    tax_cols = [i for i, h in enumerate(expanded_headers) if h == "税额"]

    if not amount_cols and not tax_cols:
        # 非发票表格（无金额/税额列），跳过
        return

    # 2. 找到合计行
    summary_row = None
    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue
        first_text = cells[0].get_text().strip()
        if first_text in ("合计", "合"):
            summary_row = row
            break
    if summary_row is None:
        return

    # 3. 提取合计行中的 ¥/￥ 值（保持原始顺序）
    yen_values: list[str] = []
    for td in summary_row.find_all("td"):
        text = td.get_text().strip()
        for m in re.finditer(r'[¥￥][\d.,]+', text):
            yen_values.append(m.group())

    if not yen_values:
        return

    # 4. 用 TH 行的 colspan 结构重建合计行
    summary_row.clear()
    for th in th_cells:
        new_td = soup.new_tag("td")
        colspan = th.get("colspan")
        if colspan:
            new_td["colspan"] = colspan
        summary_row.append(new_td)

    rebuilt_cells = summary_row.find_all("td")
    # 全部清空
    for td in rebuilt_cells:
        td.string = ""

    # 第一列放"合计"
    if rebuilt_cells:
        rebuilt_cells[0].string = "合计"

    # 5. 将 ¥ 值放置到正确列
    # ¥ 值按顺序：[金额¥, 税额¥] 或 [金额¥] 或 [金额¥, 税额¥, 金额¥2, ...]
    # 先建立目标列列表（交替：先金额后税额）
    target_pairs = []
    max_len = max(len(amount_cols), len(tax_cols))
    for i in range(max_len):
        if i < len(amount_cols):
            target_pairs.append(amount_cols[i])
        if i < len(tax_cols):
            target_pairs.append(tax_cols[i])

    for yi, yen_val in enumerate(yen_values):
        if yi >= len(target_pairs):
            logger.warning(
                f"合计行 ¥ 值数量({len(yen_values)})超过目标列数({len(target_pairs)})，"
                f"第 {yi+1} 个 ¥ 值 {yen_val} 无法放置"
            )
            break
        target_expanded = target_pairs[yi]

        # 展开列索引 → 物理列索引
        phys_idx = 0
        expanded_so_far = 0
        for ci, td in enumerate(rebuilt_cells):
            cs = int(td.get("colspan", 1))
            if expanded_so_far + cs > target_expanded:
                phys_idx = ci
                break
            expanded_so_far += cs

        if phys_idx < len(rebuilt_cells):
            rebuilt_cells[phys_idx].string = yen_val

    logger.info(
        f"合计行¥位置修复(已有TH): ¥值={yen_values}, "
        f"金额列展开={amount_cols}, 税额列展开={tax_cols}, "
        f"目标={target_pairs[:len(yen_values)]}"
    )


def extract_column_header_prefixes(html: str) -> str:
    """提取发票表格数据单元格中内嵌的列标题前缀到 <th> 表头行。

    处理 VLM 输出中列标题与数据值无空格拼接的场景：
    - "单位吨" → 提取 "单位" 到 <th>，数据行保留 "吨"
    - "数量5203" → 提取 "数量" 到 <th>，数据行保留 "5203"
    - "金额4942.85¥4942.85" → 提取 "金额" 到 <th>，数据行保留 "4942.85¥4942.85"

    与 strip_column_header_prefixes（删除前缀）不同，此函数保留全部识别内容。

    已有 <th> 行的表格（如 VLM 直接输出的发票表），跳过前缀提取，
    但会修复合计行中 ¥ 值的列位置（VLM 可能将 ¥ 值放在错误的展开列）。

    [自定义] 此函数由 _format_embedded_html 管道调用。
    上游合并时此模块仅需保留，无需修改。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        处理后的 HTML 字符串。
    """
    if not html or not isinstance(html, str):
        return html
    if "<table" not in html.lower():
        return html

    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过列标题提取")
        return html

    for table in soup.find_all("table"):
        try:
            # 已有 <th> 的表：跳过后面的前缀提取，但需修复合计行 ¥ 值列位置
            if table.find("th"):
                _fix_summary_row_yen_for_th_table(table, soup)
                continue

            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # 用 _INVOICE_DATA_COLUMN_KEYWORDS 检测拼接行
            # 跳过首行（购买方/销售方信息行），从第2行开始找
            data_row = None
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                # 检测是否有 ≥2 个单元格以列关键词开头且有后缀数据
                concat_cells = 0
                for td in cells:
                    text = td.get_text().strip()
                    if not text:
                        continue
                    m = _match_data_column_keyword(text)
                    if m and m[1]:
                        concat_cells += 1
                if concat_cells >= 2:
                    data_row = row
                    break

            if data_row is None:
                continue

            # 提取标题前缀构建 TH 行，剥离数据行前缀
            cells = data_row.find_all("td")
            header_labels: list[str] = []
            for td in cells:
                text = td.get_text().strip()
                colspan = int(td.get("colspan", 1))
                label = ""
                data = text
                m = _match_data_column_keyword(text)
                if m:
                    label, data = m
                # 按 colspan 展开：每个物理列一个 header_label（用于对齐）
                for _ in range(colspan):
                    header_labels.append(label if _ == 0 else "")
                if colspan > 1 and label:
                    label = ""  # 清空避免后续重复使用
                td.string = data

            # 插入 TH 行（仅当有 ≥2 个有效标签时）
            valid_labels = [l for l in header_labels if l]
            if len(valid_labels) >= 2:
                header_tr = soup.new_tag("tr")
                for label in header_labels:
                    th = soup.new_tag("th")
                    th.string = label
                    header_tr.append(th)
                data_row.insert_before(header_tr)
                logger.debug(
                    f"提取列标题前缀到 TH 行：{len(valid_labels)} 个标签，"
                    f"标签={valid_labels[:4]}..."
                )

                # 清理其余所有行中残留的列标题前缀
                # （如 split_summary_from_data_cell 创建的合计行复制了原始拼接文本）
                for row in table.find_all("tr"):
                    for td in row.find_all("td"):
                        text = td.get_text().strip()
                        if not text:
                            continue
                        m = _match_data_column_keyword(text)
                        if m and m[1] and _is_data_value(m[1]):
                            td.string = m[1]

                # 从数据行单元格中提取 ¥/￥ 金额值并移至合计行
                # 避免 ¥ 值在数据行和合计行重复出现
                yen_map: dict[int, str] = {}  # {expanded_col_index: yen_value}
                all_rows = table.find_all("tr")
                for row in all_rows:
                    if row.find("th"):
                        continue  # 跳过 TH 行
                    # 跳过价税合计/大写行和合计行本身
                    row_texts = [c.get_text() for c in row.find_all("td")]
                    if any("价税合计" in t or "大写" in t for t in row_texts):
                        continue
                    if row_texts and row_texts[0] == "合计":
                        continue  # 跳过合计行自身，避免从其中重复提取 ¥
                    cells = row.find_all("td")
                    expanded_idx = 0  # 按 colspan 展开后的列索引
                    for ci, td in enumerate(cells):
                        text = td.get_text().strip()
                        colspan = int(td.get("colspan", 1))
                        yen_match = re.search(r'[¥￥][\d.,]+', text) if text else None
                        if yen_match:
                            yen_val = yen_match.group()
                            # 从单元格中移除 ¥ 值
                            cleaned = (text[:yen_match.start()] + text[yen_match.end():]).strip()
                            td.string = cleaned if cleaned else ""
                            # 记录 ¥ 值及其展开后的列索引
                            yen_map[expanded_idx] = yen_val
                        expanded_idx += colspan

                if yen_map:
                    # 查找或创建合计行
                    summary_row = None
                    for row in all_rows:
                        cells_text = [c.get_text().strip() for c in row.find_all("td")]
                        if cells_text and cells_text[0] == "合计":
                            summary_row = row
                            break

                    if summary_row is None:
                        summary_row = soup.new_tag("tr")
                        # 插入到数据行之后（TH 行是倒数第二个之前）
                        data_rows = [r for r in all_rows if not r.find("th")]
                        if len(data_rows) >= 2:
                            data_rows[0].insert_after(summary_row)
                        elif data_rows:
                            data_rows[0].insert_after(summary_row)

                    # 用 TH 行的结构重建合计行（确保 colspan 对齐）
                    summary_row.clear()
                    ref_cells = header_tr.find_all("th")
                    for th in ref_cells:
                        new_td = soup.new_tag("td")
                        colspan = th.get("colspan")
                        if colspan:
                            new_td["colspan"] = colspan
                        summary_row.append(new_td)

                    # 在合计行第一列放"合计"标签，按展开列索引放 ¥ 值
                    summary_cells = summary_row.find_all("td")
                    # 先全部清空
                    for sc in summary_cells:
                        sc.string = ""
                    if summary_cells:
                        summary_cells[0].string = "合计"
                    for expanded_ci, yen_val in yen_map.items():
                        if expanded_ci < len(summary_cells):
                            summary_cells[expanded_ci].string = yen_val

        except Exception:
            logger.exception("extract_column_header_prefixes 处理单个表格时出错，跳过")
            continue

    return str(soup)


# -- 增值税发票检测关键词 --
_INVOICE_DETECTION_KEYWORDS = {
    "项目名称", "规格型号", "单位", "数量", "单价",
    "金额", "税额", "税率/征收率", "价税合计",
}


def _is_invoice_table(table: Tag) -> bool:
    """检测表格是否为增值税发票样式。

    通过检查表头行是否包含增值税发票的特征关键词列来判断。
    需要至少匹配 3 个特征关键词。

    Args:
        table: BeautifulSoup <table> Tag。

    Returns:
        True 表示检测为发票表格。
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return False

    # 在任意行中查找发票特征关键词（不限于第一行）
    all_header_texts = set()
    for row in rows:
        for cell in row.find_all(["th"]):
            text = cell.get_text().strip()
            if text:
                all_header_texts.add(text)
        # 也检查类表头行（所有单元格都是短文本的 <td> 行）
        td_cells = row.find_all("td")
        if td_cells:
            texts = [c.get_text().strip() for c in td_cells]
            if texts and all(
                len(t) < 15 and not _is_data_value(t)
                for t in texts if t
            ):
                all_header_texts.update(t for t in texts if t)
        # 只要已收集到足够关键词即可提前退出
        if len(all_header_texts & _INVOICE_DETECTION_KEYWORDS) >= 3:
            break

    # 匹配发票特征关键词
    match_count = len(
        all_header_texts & _INVOICE_DETECTION_KEYWORDS
    )

    # 也检查正文中是否有发票特有元素
    if match_count < 3:
        all_text = table.get_text()
        if "价税合计" in all_text:
            match_count += 2
        if "纳税人识别号" in all_text:
            match_count += 1

    return match_count >= 3


_SPARSE_EMPTY_RATIO_THRESHOLD = 0.3


def _is_structurally_sparse_table(table: Tag) -> bool:
    """判断表格是否为结构性稀疏表（如财务报表、征信报告）。

    这类表格的空单元格是合法留白（矩阵稀疏），非 VLM 遗漏。
    且表头不含"金额/税额/数量/单价"等关键词，列类型全部归为 "text"，
    _fill_empty_cells_from_ocr_grid 的类型匹配退化为"任意文本填任意空列"，
    会灌入表头文字/行标签，产生重复内容。因此跳过 OCR 补充。

    Args:
        table: BeautifulSoup <table> Tag。

    Returns:
        True 表示空单元格占比过高，判定为结构性稀疏。
    """
    tds = table.find_all("td")
    if not tds:
        return False
    empty_count = sum(1 for c in tds if not c.get_text().strip())
    return (empty_count / len(tds)) > _SPARSE_EMPTY_RATIO_THRESHOLD


# 财务报表表头标记：中国标准化财务报表含「行次」（会企03表现金流量表）或
# 「附注编号」（会企01/02表资产负债表/利润表，附注编号列标注科目对应附注）。
_FINANCIAL_STATEMENT_MARKERS = {"行次", "附注编号"}


def _is_financial_statement_table(table: Tag) -> bool:
    """判断表格是否为财务报表样式（表头含「行次」或「附注编号」列）。

    中国标准化财务报表含「行次」列（会企03表现金流量表）或「附注编号」列
    （会企01/02表资产负债表/利润表），用于标注科目/项目的行号或对应附注编号。
    这类表格的空单元格是合法留白（未发生业务的行次/金额为空或「-」），非 VLM
    遗漏；VLM 对结构化报表的识别已足够准确。若对其做 OCR 补充，会因 OCR 行对齐
    错位把截断标签/合并数字/单字噪声灌进空列（原则 4：信任上游正确输出，
    不过度后处理）。

    Args:
        table: BeautifulSoup <table> Tag。

    Returns:
        是否为财务报表样式表格。
    """
    # 仅看前 3 行（表头区域），去除空白后匹配「行次」或「附注编号」标记
    for tr in table.find_all("tr")[:3]:
        for cell in tr.find_all(["td", "th"]):
            if "".join(cell.get_text().split()) in _FINANCIAL_STATEMENT_MARKERS:
                return True
    return False


def _has_colspan_mismatch(html: str) -> bool:
    """快速检测表格是否存在 colspan 不一致的问题。

    若所有行的 colspan 总和相同，则无需规范化。

    Args:
        html: 表格 HTML 字符串。

    Returns:
        True 表示存在不一致（需要规范化）。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        col_sums = set()
        for row in rows:
            total = sum(
                int(c.get("colspan", 1))
                for c in row.find_all(["td", "th"])
            )
            col_sums.add(total)

        if len(col_sums) > 1:
            return True

    return False


# ============================================================
# Hybrid 模式表格 OCR 补全（方案 B：Pipeline OCR 补充 VLM 表格）
# ============================================================

def supplement_empty_table_cells(
    vlm_html: str,
    ocr_results: list,
    table_img_width: int = 0,
) -> str:
    """使用 PaddleOCR 文字网格补充 VLM 表格中的空单元格。

    VLM 能正确识别表格结构但可能遗漏个别单元格文字（如税率值），
    而 PaddleOCR 对文字识别精度很高。此函数将两者合并：
    - VLM HTML 提供表格结构（行列布局、colspan/rowspan）
    - PaddleOCR 提供精准的文字内容
    - 将 OCR 文字按坐标聚类成行列网格，填充到 VLM HTML 的空单元格中

    算法：
    1. 解析 VLM HTML → 提取每个单元格的文本和行列位置
    2. 将 OCR 结果按 y 坐标聚类分行，再按 x 坐标排序得列 → 形成 OCR 网格
    3. OCR 网格与 VLM 网格执行对齐匹配
    4. 若 VLM 单元格为空且 OCR 网格对应位置有文字 → 填充

    Args:
        vlm_html: VLM 模型生成的表格 HTML 字符串。
        ocr_results: PaddleOCR (det+rec) 的输出结果，
            格式为 [[box_points, ['text', score]], ...]。
        table_img_width: 表格图片的像素宽度，用于 x 坐标列的聚类半径计算。

    Returns:
        补充后的 HTML 字符串；若无需补充则返回原字符串。
    """
    if not vlm_html or not ocr_results:
        return vlm_html

    try:
        soup = BeautifulSoup(vlm_html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过 OCR 补充")
        return vlm_html

    # 构建 OCR 网格
    ocr_grid = _build_ocr_text_grid(ocr_results, table_img_width)
    if not ocr_grid:
        return vlm_html

    # 找到所有表格
    modified = False
    for table in soup.find_all("table"):
        try:
            modified = _fill_empty_cells_from_ocr_grid(
                soup, table, ocr_grid
            ) or modified
        except Exception:
            logger.exception("OCR 网格填充单表时出错，跳过此表格")
            continue

    if modified:
        return str(soup)
    return vlm_html


def _build_ocr_text_grid(
    ocr_results: list,
    table_img_width: int = 0,
) -> list[list[str]]:
    """将 PaddleOCR 结果按行列聚类为二维文字网格。

    步骤：
    1. 提取每个 OCR 项的文本和 bbox 中心点
    2. 按 y 坐标聚类分行（使用相邻文字行的 y 间距中位数作为聚类阈值）
    3. 每行内按 x 坐标排序

    Args:
        ocr_results: PaddleOCR 的 det+rec 输出。
        table_img_width: 表格图片宽度（像素），用于估算列聚类半径。

    Returns:
        二维文字网格 list[list[str]]，grid[row][col] = 文字。
    """
    if not ocr_results:
        return []

    # 提取 (x_center, y_center, text) 三元组
    items = []
    for res in ocr_results:
        try:
            bbox = res[0]
            text_info = res[1]
            if isinstance(text_info, (list, tuple)):
                text = str(text_info[0]) if text_info[0] else ""
            else:
                text = str(text_info) if text_info else ""

            if not text.strip():
                continue

            # 计算 bbox 中心点
            if isinstance(bbox[0], (list, tuple)):
                # 四点格式 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
            else:
                # 扁平格式 [x1,y1,x2,y2,...]
                xs = [bbox[i] for i in range(0, len(bbox), 2)]
                ys = [bbox[i] for i in range(1, len(bbox), 2)]

            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            box_height = max(ys) - min(ys)
            items.append((cx, cy, box_height, text.strip()))
        except (IndexError, TypeError, ValueError):
            continue

    if not items:
        return []

    # 按 y 坐标排序
    items.sort(key=lambda it: it[1])

    # 按 y 坐标相似度聚类分行
    # 同一行的文字具有接近的 y 坐标，不同行的文字有显著不同的 y 坐标。
    # 使用自适应阈值：以 OCR 文本框高度的中位数估算行高，同一行文字
    # 中心点 y 差异通常不超过行高的一半。固定 10px 对图像缩放不敏感
    # （大图会过度拆分、小图会过度合并），自适应阈值更稳健。
    heights = [it[2] for it in items]
    median_height = sorted(heights)[len(heights) // 2]
    y_tolerance = max(4.0, min(median_height * 0.6, 30.0))
    rows = []
    current_row = [items[0]]
    for item in items[1:]:
        current_avg_y = sum(it[1] for it in current_row) / len(current_row)
        if abs(item[1] - current_avg_y) <= y_tolerance:
            current_row.append(item)
        else:
            rows.append(current_row)
            current_row = [item]
    rows.append(current_row)

    # 每行内按 x 排序
    grid = []
    for row in rows:
        row.sort(key=lambda it: it[0])
        grid.append([it[3] for it in row])

    return grid


def _append_text_to_cell(cell_tag: Tag, text: str) -> None:
    """向表格单元格追加文本，保留已有内容（如图片标签）。

    使用 Tag.append() 在单元格末尾添加文本节点，
    避免 cell_tag.string 赋值会清除 <img> 等子元素的问题。

    Args:
        cell_tag: BeautifulSoup Tag（<td> 或 <th>）。
        text: 要追加的文本。
    """
    existing = cell_tag.get_text().strip()
    prefix = " " if existing else ""
    cell_tag.append(NavigableString(f"{prefix}{text}"))


def _parse_vlm_table_structure(rows: list[Tag]) -> tuple[list[list[str]], list[list[Tag]]]:
    """解析 VLM 表格结构，展开 colspan 生成等宽网格。

    Args:
        rows: <tr> 标签列表。

    Returns:
        (vlm_data, vlm_cells): 文本网格和对应的 Tag 网格。
        colspan 单元格被重复展开为等宽表示。
    """
    vlm_data = []
    vlm_cells = []
    for row in rows:
        row_cells = row.find_all(["td", "th"])
        if not row_cells:
            continue
        row_texts = []
        row_tags = []
        for cell in row_cells:
            colspan = int(cell.get("colspan", 1))
            text = cell.get_text().strip()
            for _ in range(colspan):
                row_texts.append(text)
                row_tags.append(cell)
        vlm_data.append(row_texts)
        vlm_cells.append(row_tags)
    return vlm_data, vlm_cells


def _detect_data_row_start(rows: list[Tag], vlm_data: list[list[str]]) -> int:
    """检测数据行起始位置。

    表头是表格开头的连续块。规则：
    1. 含 <th> 的行视为表头。
    2. 含 <img> 的行视为数据行（含手写/签章图片），不参与表头扩展。
    3. 含数值的行视为数据行，终止表头扩展——表头不含裸数值
       （"2022年度"因含"年度"二字，_is_data_value 判为 False）。
    4. 全短标签行（<15 字且非数据值）仅在表头块内视为类表头。

    Args:
        rows: <tr> 标签列表。
        vlm_data: 展开后的文本网格。

    Returns:
        第一个数据行的索引。
    """
    data_row_start = 0
    for i, row in enumerate(rows):
        if row.find("th"):
            data_row_start = i + 1
        elif row.find("img"):
            # 含图片的行是数据行，终止表头扩展
            break
        else:
            cells = row.find_all("td")
            texts = [c.get_text().strip() for c in cells]
            # 含数值 → 已进入数据区，终止表头判定。
            # 防止"数值列本为空的标签数据行"（如所有者权益变动表中间的
            # "加:会计政策变更""1.提取盈余公积"等）被误判为表头，
            # 导致 data_row_start 一路推进到表格末尾行。
            if any(_is_data_value(t) for t in texts if t):
                break
            if texts and all(
                len(t) < 15 and not _is_data_value(t)
                for t in texts if t
            ):
                data_row_start = i + 1
    return data_row_start


def _normalize_for_matching(text: str) -> str:
    """规范化文本用于模糊匹配。

    将 OCR 输出中常见的全角标点转换为半角，
    解决 VLM（半角）与 OCR（全角）之间的字符编码差异。

    Args:
        text: 待规范化的文本。

    Returns:
        规范化后的文本。
    """
    full_to_half = {
        "（": "(", "）": ")", "：": ":", "，": ",",
        "。": ".", "！": "!", "？": "?", "；": ";",
        "“": '"', "”": '"', "【": "[", "】": "]",
        "《": "<", "》": ">", "％": "%", "＋": "+",
        "－": "-", "＝": "=", "０": "0", "１": "1",
        "２": "2", "３": "3", "４": "4", "５": "5",
        "６": "6", "７": "7", "８": "8", "９": "9",
        " ": " ", "　": " ",
        "￥": "¥",   # OCR 全角人民币符号 → VLM 半角
    }
    result = text
    for full, half in full_to_half.items():
        result = result.replace(full, half)
    return result


def _align_ocr_to_vlm_rows(
    ocr_grid: list[list[str]],
    vlm_data: list[list[str]],
    data_row_start: int,
) -> dict[int, list[str]]:
    """使用标签锚点将 OCR 网格行对齐到 VLM 数据行。

    OCR 网格（由 _build_ocr_text_grid 按 y 坐标聚类生成）的行数通常
    多于 VLM 表格的行数。此函数通过标签子串匹配将 OCR 行分配到对应的
    VLM 数据行，形成每个 VLM 行的 OCR 文本池。

    匹配规则：
    - 若 OCR 行中含某 VLM 数据行的标签文本（规范化后子串匹配）→ 锚定到该 VLM 行
    - 若无标签匹配 → 向前看一行：若下一行是标签 → 使用下一行的 VLM 行
    - 否则 → 跟随上一个锚定的 VLM 行

    Args:
        ocr_grid: OCR 识别的文字网格。
        vlm_data: 展开后的 VLM 文本网格。
        data_row_start: 数据行起始索引。

    Returns:
        {vlm_row_idx: [ocr_text, ...]} 映射。
    """
    vlm_nrows = len(vlm_data)
    ocr_pool: dict[int, list[str]] = {
        vi: [] for vi in range(data_row_start, vlm_nrows)
    }
    if not ocr_pool:
        return ocr_pool

    # 预先规范化 VLM 数据行文本
    vlm_norm: list[list[str]] = [
        [_normalize_for_matching(t) for t in row] for row in vlm_data
    ]

    current_vlm_row = data_row_start

    for oi, ocr_row in enumerate(ocr_grid):
        # 查找该 OCR 行最匹配的 VLM 数据行
        best_row = None
        best_score = 0
        for vi in range(data_row_start, vlm_nrows):
            score = 0
            for ot in ocr_row:
                if not ot:
                    continue
                ot_norm = _normalize_for_matching(ot)
                for vt_norm in vlm_norm[vi]:
                    if vt_norm and (ot_norm in vt_norm or vt_norm in ot_norm):
                        # 匹配长度加权：越长匹配越可靠
                        score += min(len(ot_norm), len(vt_norm))
            if score > best_score:
                best_score = score
                best_row = vi

        if best_row is not None:
            current_vlm_row = best_row
        elif oi + 1 < len(ocr_grid):
            # 向前看一行：OCR 识别中值文本常出现在标签文本上方（存单/票据模式）
            # 守卫条件：仅当当前行是"稀疏文本行"（≤2项，全部为 text 类型）时才 peek-ahead
            # 防止发票场景中将纯数值行（¥226.42, ¥29.43）错误前推到合计行
            curr_non_empty = [t for t in ocr_row if t]
            curr_types = [_classify_ocr_item_type(t) for t in curr_non_empty]
            is_sparse_text = (
                len(curr_non_empty) <= 2
                and all(t == "text" for t in curr_types)
            ) if curr_types else False

            if is_sparse_text:
                # 若下一行有标签匹配，则当前值归属于下一行所属的 VLM 行
                next_row = ocr_grid[oi + 1]
                next_best = None
                next_score = 0
                for vi in range(data_row_start, vlm_nrows):
                    score = 0
                    for ot in next_row:
                        if not ot:
                            continue
                        ot_norm = _normalize_for_matching(ot)
                        for vt_norm in vlm_norm[vi]:
                            if vt_norm and (ot_norm in vt_norm or vt_norm in ot_norm):
                                score += min(len(ot_norm), len(vt_norm))
                    if score > next_score:
                        next_score = score
                        next_best = vi
                if next_best is not None:
                    current_vlm_row = next_best

        if current_vlm_row in ocr_pool:
            ocr_pool[current_vlm_row].extend([t for t in ocr_row if t])

    return ocr_pool


def _get_fillable_columns(
    vlm_row: list[str],
    vlm_tag_row: list[Tag],
    header_types: dict[int, str],
) -> list[tuple[int, str, str]]:
    """找出 VLM 行中可填充的列。

    Args:
        vlm_row: 展开后的文本行。
        vlm_tag_row: 展开后的 Tag 行。
        header_types: 列类型映射。

    Returns:
        [(col_idx, column_type, mode), ...] 列表。
        mode: "append"（含图片的单元格，追加值）或 "empty"（完全空的单元格）。
    """
    fillable = []
    for vc in range(len(vlm_row)):
        text = vlm_row[vc].strip()
        cell_tag = vlm_tag_row[vc]
        has_img = cell_tag.find("img") is not None
        col_type = header_types.get(vc, "text")

        if has_img:
            # 含图片的单元格 → 追加 OCR 识别的值文本
            fillable.append((vc, col_type, "append"))
        elif not text:
            # 纯空单元格 → 可直接填充
            fillable.append((vc, col_type, "empty"))
    return fillable


def _count_ocr_data_rows(ocr_grid: list[list[str]]) -> int:
    """统计 OCR 网格中包含数据值的行数。

    数据值判定：至少含一个 _is_data_value 返回 True 的单元格。

    Args:
        ocr_grid: OCR 识别文字网格。

    Returns:
        数据行数量。
    """
    return sum(
        1 for row in ocr_grid
        if any(_is_data_value(cell) for cell in row)
    )


# 发票明细列关键词（仅数据行，不含购买方/销售方等摘要标签）
_INVOICE_DATA_COLUMN_KEYWORDS = {
    "项目名称", "货物或应税劳务、服务名称", "规格型号", "单位",
    "数量", "单价", "金额", "税率", "税额", "税率/征收率",
}


def _match_data_column_keyword(text: str) -> tuple[str, str] | None:
    """识别文本开头的发票数据列关键词，容忍关键词内部空格。

    VLM 对纵向排版的表头（如"数量""单价"上下两字）会输出为"数 量"、
    "单 价"（关键词内部含空格）。先按原文本直接前缀匹配（保留数据值内
    空格），失败时再用去除全部空白后的紧凑文本匹配。

    Args:
        text: 单元格文本（已 strip）。

    Returns:
        (keyword, data) 元组；未匹配返回 None。data 为去除关键词前缀后的
        剩余文本（空字符串表示纯关键词单元格）。
    """
    compact = "".join(text.split())
    for kw in sorted(_INVOICE_DATA_COLUMN_KEYWORDS, key=len, reverse=True):
        # 纯关键词单元格（如独立的"规格型号"）
        if text == kw:
            return kw, ""
        # 直接前缀匹配（如"单位吨"→"单位"+"吨"）
        if text.startswith(kw) and len(text) > len(kw):
            rest = text[len(kw):].strip()
            if rest:
                return kw, rest
        # 关键词内部含空格（如"数 量5203"→"数量"+"5203"）
        if compact != text and compact.startswith(kw) and len(compact) > len(kw):
            rest = compact[len(kw):].strip()
            if rest:
                return kw, rest
    return None


# 发票数值列关键词：用于判断 VLM 拼接列是否为数值列。
# 与 _is_data_value（仅识别纯数字/¥/% token）不同，此集合从"列语义"出发，
# 能正确识别 "金额4071.26¥37744.85"、"税率3%3%"、"税 额122.14¥1132.35"
# 这类"列关键词 + 拼接数值"单元格为数值列。
_NUMERIC_COLUMN_KEYWORDS = {"数量", "单价", "金额", "税率", "税额"}


def _is_numeric_column(text: str) -> bool:
    """判断单元格文本是否为发票数值列（数量/单价/金额/税率/税额）。

    基于列关键词前缀判断，而非 _is_data_value 的数值 token 判断。
    _is_data_value 无法识别拼接单元格（如 "金额4071.26¥37744.85" 因含 ¥
    返回 False、"税率3%3%" 因含 % 返回 False、"税 额122.14..." 因含空格
    返回 False），导致这些列被误判为非数值列，类型匹配失效。

    Args:
        text: 单元格文本（已 strip）。

    Returns:
        True 表示该列为数值列。
    """
    m = _match_data_column_keyword(text.strip())
    return m is not None and m[0] in _NUMERIC_COLUMN_KEYWORDS


def _try_split_concatenated_numbers(text: str, num_parts: int = 2) -> list[str]:
    """尝试拆分 OCR 中因间距过近而合并的连续数值。

    VLM/OCR 将相邻列的两个数值合并为一个字符串（如 "5203.001.40776667307"
    实际是数量 "5203.00" + 单价 "1.40776667307"）。

    策略：利用小数位数启发式——第一个数值通常有 0-2 位小数
    （数量/金额），第二个数值可以有多位小数（单价/税率）。

    Args:
        text: 合并的数值字符串。
        num_parts: 期望拆分的份数。

    Returns:
        拆分后的数值列表；若无法拆分则返回 [text]。
    """
    if not text or num_parts < 2:
        return [text] if text else []

    # 尝试不同小数位数：2位 → 1位 → 0位
    for frac_digits in (2, 1, 0):
        pat = re.compile(
            rf'(\d+(?:\.\d{{{frac_digits}}})?)(\d+\.\d+.*)'
            if frac_digits > 0
            else r'(\d+)(\d+\.\d+.*)'
        )
        m = pat.match(text.strip())
        if m:
            first = m.group(1)
            rest = m.group(2)
            # 验证：第一部分和第二部分都应像数值
            if _is_data_value(first) and _is_data_value(rest):
                parts = [first]
                for i in range(num_parts - 2):
                    sub = _try_split_concatenated_numbers(rest, num_parts - 1)
                    if len(sub) > 1:
                        parts.extend(sub[:-1])
                        rest = sub[-1]
                        break
                parts.append(rest)
                return parts

    return [text]


def _split_decimal_values(data: str, n_data: int) -> list[str]:
    """按等精度拆分多小数位拼接数值（如单价）。

    单价等列的小数位数固定（如 11 位），拼接如
    "1.407766251722.11650471401" 实际是两个等精度数值。以最后一个小数
    点后的位数作为统一小数位数，按此切分各数值的整数/小数边界。

    Args:
        data: 拼接的十进制数值文本。
        n_data: 期望的数值个数。

    Returns:
        拆分后的数值列表；无法可靠拆分返回空列表。
    """
    if not data or n_data < 2:
        return []
    dots = [i for i, ch in enumerate(data) if ch == "."]
    if len(dots) != n_data:
        return []
    frac_len = len(data) - dots[-1] - 1
    if frac_len < 1:
        return []
    vals: list[str] = []
    start = 0
    for i, dot in enumerate(dots):
        end = dot + 1 + frac_len if i < n_data - 1 else len(data)
        # 非末值：切分边界必须落在下一个小数点之前（整数部分为空/越界即不合理）
        if i < n_data - 1 and end >= dots[i + 1]:
            return []
        val = data[start:end]
        if not re.fullmatch(r"\d+\.\d+", val):
            return []
        vals.append(val)
        start = end
    return vals


def _split_name_cell(data: str, n_data: int) -> tuple[list[str], str]:
    """拆分货物名称拼接单元格为 n_data 个服务名称 + 合计标签。

    VAT 发票货物名称形如 "*品类*序号-名称"（品类如"水冰雪""劳务"），
    多行明细会拼接为 "*水冰雪*1-居民生活*水冰雪*5-生产合 计"。以
    "*品类*" 段落为单位拆分服务名称，末尾的"合计/小计"识别为合计标签。

    Args:
        data: 拼接的名称文本。
        n_data: 期望的数据行数。

    Returns:
        (names, summary) 元组；names 长度不等于 n_data 时返回 ([], "")。
    """
    summary = ""
    m = re.search(r"(合\s*计|小\s*计)\s*$", data)
    if m:
        summary = "合计"
        data = data[: m.start()].strip()
    names = re.findall(r"\*[^*]+\*[^*]+", data)
    if len(names) != n_data:
        return [], ""
    return names, summary


def _has_concatenated_data_cells(vlm_data: list[list[str]]) -> bool:
    """检测 VLM 表格行中是否存在值拼接的单元格。

    对每一行，检查是否有 ≥2 个单元格满足任一条件：
    1. 以发票明细列关键词开头 + 含 ≥2 个显著数值（位数≥2）
    2. 以发票明细列关键词开头 + 数字总位数 ≥8
       （如 "1810518105"→10位，正常单值"18105"→5位，拼接信号）

    不使用"合计"关键词检测——"合计"嵌入在货物名称末尾是正常场景，
    应由 split_summary_from_data_cell 处理，而非触发 OCR 行重建。

    Args:
        vlm_data: 展开后的 VLM 文本网格。

    Returns:
        True 表示至少有一行存在拼接。
    """
    import re

    for row_texts in vlm_data:
        concat_cells = 0
        for text in row_texts:
            if not text or not text.strip():
                continue
            norm_t = _normalize_for_matching(text).replace(" ", "")
            starts_with_keyword = any(
                norm_t.startswith(_normalize_for_matching(kw).replace(" ", ""))
                for kw in _INVOICE_DATA_COLUMN_KEYWORDS
            )
            if not starts_with_keyword:
                continue
            # 条件1：含 ≥2 个显著数值（位数≥2）
            nums = [n for n in re.findall(r'\d+\.?\d*', text) if len(n) >= 2]
            if len(nums) >= 2:
                concat_cells += 1
                continue
            # 条件2：单元格中数值总位数 ≥8
            # （如 "1810518105"=10位，正常单值"18105"=5位，"5203.0030088.00"=15位）
            all_digits = re.sub(r'[^\d]', '', text)
            if len(all_digits) >= 8:
                concat_cells += 1
        if concat_cells >= 2:
            return True
    return False


def _find_ocr_header_row(ocr_grid: list[list[str]]) -> int:
    """在 OCR 网格中定位表头行。

    返回第一个匹配 ≥2 个发票特征关键词的行的索引。
    若未找到，返回 0（视第一行为表头）。

    Args:
        ocr_grid: OCR 识别文字网格。

    Returns:
        表头行索引。
    """
    for i, row in enumerate(ocr_grid):
        matches = sum(
            1 for cell in row
            if cell.strip() in _INVOICE_HEADER_KEYWORDS
        )
        if matches >= 2:
            return i

    # 回退：若全文含"价税合计"，取它之前的行
    for i, row in enumerate(ocr_grid):
        all_text = " ".join(row)
        if "价税合计" in all_text:
            return max(0, i - 2)
    return 0


def _merge_split_header_chars(ocr_header: list[str]) -> list[str]:
    """合并 OCR 表头中被拆分的单个中文字符。

    PaddleOCR 在小图片上可能将多字符标签（如"金额"、"税额"、"单价"）
    检测为独立的单个字符。此函数尝试合并相邻的单个中文字符，
    仅当合并结果在 _INVOICE_HEADER_KEYWORDS 中时才会合并。

    Args:
        ocr_header: OCR 表头行的单元格文本列表。

    Returns:
        合并后的表头列表，长度可能小于输入。
    """
    if not ocr_header:
        return ocr_header

    def _is_single_cjk(c: str) -> bool:
        """判断是否为单个 CJK（中日韩统一表意文字）字符。"""
        return (
            len(c) == 1
            and ('一' <= c <= '鿿' or '㐀' <= c <= '䶿')
        )

    result: list[str] = []
    skip_next = False
    for i, cell in enumerate(ocr_header):
        if skip_next:
            skip_next = False
            continue

        stripped = cell.strip() if cell else ""

        # 尝试与下一个单元格合并（仅当两个都是单个中文字符）
        if _is_single_cjk(stripped) and i + 1 < len(ocr_header):
            next_cell = ocr_header[i + 1].strip() if ocr_header[i + 1] else ""
            if _is_single_cjk(next_cell):
                candidate = stripped + next_cell
                if candidate in _INVOICE_HEADER_KEYWORDS:
                    result.append(candidate)
                    skip_next = True
                    continue

        result.append(stripped)

    return result


def _map_ocr_cols_to_vlm_cols(
    ocr_header: list[str],
    vlm_data_row: list[str],
) -> dict:
    """将 OCR 表头列映射到 VLM 数据行的对应列。

    通过去空格 + 全角转半角后的文本子串匹配建立映射。
    仅映射在 _INVOICE_HEADER_KEYWORDS 中的 OCR 表头。

    Args:
        ocr_header: OCR 表头行的单元格文本列表。
        vlm_data_row: VLM 拼接数据行的单元格文本列表。

    Returns:
        {ocr_col_idx: vlm_col_idx} 映射字典。
    """
    def _norm(text: str) -> str:
        """去空格 + 全角转半角规范化。"""
        return _normalize_for_matching(text).replace(" ", "")

    mapping: dict[int, int] = {}
    used_vlm: set[int] = set()

    for oc, ocr_hdr in enumerate(ocr_header):
        if not ocr_hdr or ocr_hdr not in _INVOICE_HEADER_KEYWORDS:
            continue
        ocr_norm = _norm(ocr_hdr)
        if not ocr_norm:
            continue
        for vc, vlm_text in enumerate(vlm_data_row):
            if vc in used_vlm:
                continue
            vlm_norm = _norm(vlm_text)
            if ocr_norm in vlm_norm or vlm_norm in ocr_norm:
                mapping[oc] = vc
                used_vlm.add(vc)
                break

    return mapping


def _fix_ocr_summary_row_yen_position(
    new_rows: list[Tag],
    template_cells: list[Tag],
    ocr_header: list[str],
    ocr_header_labels: list[str],
    col_map: dict[int, int],
    soup: BeautifulSoup,
    ocr_data_rows: list[list[str]] | None = None,
) -> None:
    """修复 OCR 重建表格中合计行的 ¥ 值列位置。

    OCR 网格中的合计行（首列为"合计"/"合"）在经过模板列匹配后，
    ¥ 值可能被分配到错误的列——模板列文本匹配对 ¥ 值不感知列语义，
    会将 "￥57689.91" 匹配到包含该子串的任意模板列文本中。

    此函数重建合计行的 colspan 结构以匹配模板列布局，
    然后将 ¥ 值放置到正确的数值列（"金额"列、"税额"列）。

    Args:
        new_rows: OCR 重建后的所有行（会被原地修改）。
        template_cells: VLM 拼接行的物理单元格模板。
        ocr_header: OCR 表头行的单元格文本列表。
        ocr_header_labels: OCR 表头中在 _INVOICE_HEADER_KEYWORDS 内的标签。
        col_map: OCR 列索引到 VLM 列索引的映射。
        soup: BeautifulSoup 对象。
        ocr_data_rows: OCR 数据行网格（含合计行），用于从 OCR 原始合计行提取 ¥ 值。
            当 VLM 金额单元格漏掉金额合计时，步骤 4 的文本匹配会丢弃该 ¥ 值，
            此参数使本函数能回退到完整的 OCR 合计行（按 x 排序，¥ 顺序为
            [金额¥, 税额¥]）。为 None 或未找到合计行时回退到重建行提取。
    """
    # 构建展开后的列标题列表（如 expanded_headers[7] = "金额"）
    expanded_headers: list[str] = []
    for vc, orig in enumerate(template_cells):
        colspan = int(orig.get("colspan", 1))
        # 确定该物理列对应的标签
        label = ""
        vlm_tpl_norm = _normalize_for_matching(
            orig.get_text().strip()
        ).replace(" ", "")
        for oc_hdr in ocr_header_labels:
            hdr_norm = _normalize_for_matching(oc_hdr).replace(" ", "")
            if hdr_norm and vlm_tpl_norm and (
                hdr_norm in vlm_tpl_norm or vlm_tpl_norm in hdr_norm
            ):
                label = oc_hdr
                break
        if not label:
            for oc, mapped_vc in col_map.items():
                if mapped_vc == vc and oc < len(ocr_header):
                    candidate = ocr_header[oc].strip()
                    if candidate in _INVOICE_HEADER_KEYWORDS:
                        label = candidate
                        break
        for _ in range(colspan):
            expanded_headers.append(label)

    # 找到"金额"和"税额"列的展开索引
    amount_expanded_cols = [
        i for i, h in enumerate(expanded_headers) if h == "金额"
    ]
    tax_expanded_cols = [
        i for i, h in enumerate(expanded_headers) if h == "税额"
    ]

    # 优先从 OCR 原始合计行提取 ¥ 值。VLM 可能漏掉金额合计（如消防发票），
    # 步骤 4 会因此丢弃该 ¥ 值；OCR 合计行按 x 排序，¥ 值顺序为 [金额¥, 税额¥]。
    ocr_summary_yen: list[str] = []
    if ocr_data_rows:
        for orow in ocr_data_rows:
            if not orow or orow[0].strip() not in ("合计", "合"):
                continue
            for c in orow:
                for m in re.finditer(r'[¥￥][\d.,]+', c):
                    ocr_summary_yen.append(m.group())
            break

    logger.info(
        f"OCR合计行¥位置修复: expanded_headers={expanded_headers}, "
        f"金额列={amount_expanded_cols}, 税额列={tax_expanded_cols}, "
        f"new_rows数={len(new_rows)}"
    )

    for row in new_rows:
        cells = row.find_all("td")
        if not cells:
            continue
        first_text = cells[0].get_text().strip()
        logger.info(
            f"OCR合计行¥位置修复: 检查行 first_text='{first_text}', "
            f"cells数={len(cells)}"
        )
        if first_text not in ("合计", "合"):
            continue

        # 提取合计行所有 ¥/￥ 值：优先用 OCR 原始合计行（完整），
        # 回退到重建行文本（OCR 未识别到合计行或未含 ¥ 时）
        yen_values = list(ocr_summary_yen)
        if not yen_values:
            for td in cells:
                text = td.get_text().strip()
                for m in re.finditer(r'[¥￥][\d.,]+', text):
                    yen_values.append(m.group())

        if not yen_values:
            continue

        # 重建行结构：用模板列的 colspan 创建单元格
        row.clear()
        for orig in template_cells:
            new_td = soup.new_tag("td")
            cs = orig.get("colspan")
            if cs:
                new_td["colspan"] = cs
            row.append(new_td)

        rebuilt_cells = row.find_all("td")
        for td in rebuilt_cells:
            td.string = ""

        # 放置"合计"标签到第一列
        rebuilt_cells[0].string = "合计"

        # 找到每个 ¥ 值对应的展开列索引并放置
        # ¥ 值顺序通常为 [金额¥值, 税额¥值]
        for yi, yen_val in enumerate(yen_values):
            if yi == 0 and amount_expanded_cols:
                target_expanded = amount_expanded_cols[0]
            elif yi == 1 and tax_expanded_cols:
                target_expanded = tax_expanded_cols[0]
            elif yi < len(amount_expanded_cols):
                target_expanded = amount_expanded_cols[yi]
            else:
                continue

            # 展开列索引 → 物理列索引
            phys_idx = 0
            expanded_so_far = 0
            for ci, td in enumerate(rebuilt_cells):
                cs = int(td.get("colspan", 1))
                if expanded_so_far + cs > target_expanded:
                    phys_idx = ci
                    break
                expanded_so_far += cs

            if phys_idx < len(rebuilt_cells):
                rebuilt_cells[phys_idx].string = yen_val

        logger.info(
            f"OCR合计行¥位置修复: ¥值={yen_values}, "
            f"金额列展开索引={amount_expanded_cols}, "
            f"税额列展开索引={tax_expanded_cols}"
        )


def _reconstruction_preserves_vlm_values(
    template_cells: list[Tag],
    new_rows: list[Tag],
) -> bool:
    """校验 OCR 重建是否保留了 VLM 拼接单元格中的全部数值（原则 1/4/10）。

    值保真校验：VLM 拼接行虽将多行合并为单行，但其数值（数量/单价/金额/
    税率/税额）通常完整且正确。OCR 网格因 y 聚类误差可能丢失或错位这些
    数值。本函数比较重建前后数值的总"数字位数"：
    - 丢失整段数值（如丢失 "15910.00" 7 位、"2.11650471401" 12 位）时，
      数字总数显著下降；
    - 数字位数对拼接不敏感（"2892.0015910.00" 无论按何种方式拆分，
      总位数恒为 13），因此比"数值个数"更稳健。

    Args:
        template_cells: VLM 拼接行的物理单元格（含 colspan）。
        new_rows: OCR 重建后的行（含表头行，表头无数字不影响比较）。

    Returns:
        True 表示重建保留了 VLM 数值，可安全替换；False 表示存在值丢失。
    """
    def _digit_count(text: str) -> int:
        return sum(1 for ch in text if ch.isdigit())

    vlm_digits = sum(_digit_count(c.get_text()) for c in template_cells)
    rebuilt_digits = sum(
        _digit_count(c.get_text())
        for row in new_rows
        for c in row.find_all(["td", "th"])
    )

    if vlm_digits == 0:
        # VLM 拼接行不含任何数字，无值可丢失，直接放行
        return True

    # 允许 OCR 识别过程中的少量位数损失（如漏掉结尾 ".00"），
    # 但丢失整段数值（一个 5 位数量即 5 位以上）应判定为丢失。
    # 阈值 5%：数字位数损失超过 5% 即视为存在值丢失，放弃重建。
    return rebuilt_digits >= vlm_digits * 0.95


def _split_concatenated_row_deterministically(
    soup: BeautifulSoup,
    table: Tag,
    vlm_data: list[list[str]],
    data_row_start: int,
) -> bool:
    """确定性拆分 VLM 拼接行（不依赖 OCR，避免 OCR 行聚类不确定性）。

    当 VLM 将多行发票明细合并为单行且数值完整时，直接用 VLM 拼接值按
    "数据行数 N" 拆回多行（原则 4：信任 VLM 完整正确值；原则 5：以 N
    为强约束）。N 从数量列（≤2 位小数金额值个数）确定，回退税率列。
    逐列用关键词对应的规则拆分；任一列无法可靠拆分则整体返回 False，
    回退 OCR 重建。拆分后保留拼接行模板列的 colspan，并插入表头行。

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
        vlm_data: 展开后的 VLM 文本网格。
        data_row_start: VLM 数据行起始索引。

    Returns:
        True 表示已确定性拆分并替换原拼接行。
    """
    rows = table.find_all("tr")

    # 1. 定位拼接行（含 ≥2 个数值拼接单元格）
    concat_row_idx = -1
    for vi in range(max(0, data_row_start), len(rows)):
        concat_count = 0
        for t in [c.get_text().strip() for c in rows[vi].find_all(["td", "th"])]:
            if not t:
                continue
            m = _match_data_column_keyword(t)
            if m is None or not m[1]:
                continue
            if m[0] in _NUMERIC_COLUMN_KEYWORDS:
                if (
                    len([n for n in re.findall(r"\d+\.?\d*", m[1]) if len(n) >= 2]) >= 2
                    or len(re.sub(r"[^\d]", "", m[1])) >= 8
                ):
                    concat_count += 1
        if concat_count >= 2:
            concat_row_idx = vi
            break
    if concat_row_idx < 0:
        return False

    template_cells = rows[concat_row_idx].find_all(["td", "th"])
    if not template_cells:
        return False

    # 2. 解析每列关键词与拼接数据
    parsed: list[tuple[int, str, str]] = []
    for vc, cell in enumerate(template_cells):
        text = cell.get_text().strip()
        m = _match_data_column_keyword(text)
        if m is not None:
            parsed.append((vc, m[0], m[1].strip()))
        else:
            parsed.append((vc, "", text))

    # 3. 确定数据行数 N（优先数量列，其次税率列）
    n_data = 0
    for _, kw, data in parsed:
        if kw == "数量":
            vals = re.findall(r"\d+\.\d{2}", data)
            if vals:
                n_data = len(vals)
                break
    if n_data < 2:
        for _, kw, data in parsed:
            if kw == "税率":
                vals = re.findall(r"\d+(?:\.\d+)?\s*%", data)
                if vals:
                    n_data = len(vals)
                    break
    if n_data < 2:
        logger.debug("确定性拆分无法确定数据行数 N，跳过")
        return False

    # 4. 逐列拆分
    col_values: dict[int, list[str]] = {}
    summary_values: dict[int, str] = {}
    summary_name = ""
    for vc, kw, data in parsed:
        if kw == "数量":
            vals = re.findall(r"\d+\.\d{2}", data)
            if len(vals) != n_data:
                return False
            col_values[vc] = vals
        elif kw == "税率":
            vals = re.findall(r"\d+(?:\.\d+)?\s*%", data)
            if len(vals) != n_data:
                return False
            col_values[vc] = vals
        elif kw == "单价":
            vals = _split_decimal_values(data, n_data)
            if len(vals) != n_data:
                return False
            col_values[vc] = vals
        elif kw in ("金额", "税额"):
            yen = re.findall(r"[¥￥][\d.,]+", data)
            data_no_yen = re.sub(r"[¥￥][\d.,]+", "", data)
            vals = re.findall(r"\d+\.\d{2}", data_no_yen)
            if len(vals) != n_data or len(yen) > 1:
                return False
            col_values[vc] = vals
            summary_values[vc] = yen[0] if yen else ""
        elif kw in ("项目名称", "货物或应税劳务、服务名称"):
            names, summary = _split_name_cell(data, n_data)
            if len(names) != n_data:
                return False
            col_values[vc] = names
            summary_name = summary or "合计"
        elif kw == "单位":
            if len(data) >= n_data and len(data) % n_data == 0:
                k = len(data) // n_data
                col_values[vc] = [data[i * k : (i + 1) * k] for i in range(n_data)]
            else:
                col_values[vc] = [""] * n_data
        else:
            # 规格型号等：置空（发票常无此列值）
            col_values[vc] = [""] * n_data

    # 5. 构建 N 个数据行 + 1 个合计行（沿用模板列 colspan）
    new_rows: list[Tag] = []
    for i in range(n_data):
        tr = soup.new_tag("tr")
        for vc, orig in enumerate(template_cells):
            td = soup.new_tag("td")
            for attr in ("colspan", "rowspan"):
                if orig.get(attr):
                    td[attr] = orig[attr]
            td.string = col_values.get(vc, [""] * n_data)[i]
            tr.append(td)
        new_rows.append(tr)

    summary_tr = soup.new_tag("tr")
    for vc, orig in enumerate(template_cells):
        td = soup.new_tag("td")
        for attr in ("colspan", "rowspan"):
            if orig.get(attr):
                td[attr] = orig[attr]
        if vc in summary_values and summary_values[vc]:
            td.string = summary_values[vc]
        elif parsed[vc][1] in ("项目名称", "货物或应税劳务、服务名称"):
            td.string = summary_name
        else:
            td.string = ""
        summary_tr.append(td)
    new_rows.append(summary_tr)

    # 6. 插入表头行（用拼接行模板列 + 关键词标签，保留 colspan）
    header_tr = soup.new_tag("tr")
    for vc, orig in enumerate(template_cells):
        th = soup.new_tag("th")
        for attr in ("colspan", "rowspan"):
            if orig.get(attr):
                th[attr] = orig[attr]
        kw = parsed[vc][1]
        th.string = kw if kw else orig.get_text().strip()
        header_tr.append(th)
    new_rows.insert(0, header_tr)

    # 7. 替换原拼接行
    prev = rows[concat_row_idx - 1] if concat_row_idx > 0 else None
    rows[concat_row_idx].decompose()
    if prev is not None:
        target = prev
        for tr in reversed(new_rows):
            target.insert_after(tr)
    else:
        first = table.find("tr")
        if first is not None:
            for tr in reversed(new_rows):
                first.insert_before(tr)
        else:
            for tr in new_rows:
                table.append(tr)

    logger.info(f"确定性拆分 VLM 拼接行：{n_data} 数据行 + 1 合计行")
    return True


def _rebuild_merged_rows_from_ocr(
    soup: BeautifulSoup,
    table: Tag,
    ocr_grid: list[list[str]],
    vlm_data: list[list[str]],
    vlm_cells: list[list[Tag]],
    data_row_start: int,
) -> bool:
    """用 OCR 网格重建被 VLM 合并的数据行。

    当 VLM 将多行数据合并为单个 <tr> 时，此函数使用 OCR 网格
    （由 PaddleOCR 按视觉 y 坐标聚类生成）作为真实行结构来重建。

    算法：
    1. 在 OCR 网格中定位表头行和数据行
    2. 建立 OCR 列到 VLM 列的映射
    3. 为每个 OCR 数据行创建独立的 <tr>
    4. 替换原 VLM 拼接行

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
        ocr_grid: OCR 识别文字网格。
        vlm_data: 展开后的 VLM 文本网格。
        vlm_cells: 展开后的 VLM Tag 网格。
        data_row_start: VLM 数据行起始索引。

    Returns:
        True 表示重建成功。
    """
    rows = table.find_all("tr")
    if data_row_start >= len(rows):
        return False

    # 1. 定位 OCR 表头和数据行
    header_row_idx = _find_ocr_header_row(ocr_grid)
    if header_row_idx >= len(ocr_grid) - 1:
        logger.debug(
            f"OCR 网格中未找到有效的表头行（header_row_idx={header_row_idx}, "
            f"ocr_grid_len={len(ocr_grid)}），跳过重建"
        )
        return False

    # 数据行：表头之后到"价税合计"/"合计"之前
    ocr_data_rows: list[list[str]] = []
    for i in range(header_row_idx + 1, len(ocr_grid)):
        row_text = " ".join(ocr_grid[i])
        if "价税合计" in row_text or (
            len(ocr_data_rows) > 0 and "合计" in row_text
            and not any(_is_data_value(c) for c in ocr_grid[i] if c != "合计")
        ):
            break
        ocr_data_rows.append(ocr_grid[i])

    if not ocr_data_rows:
        logger.debug(f"OCR 网格中未找到数据行（header_row_idx={header_row_idx}, ocr_grid_len={len(ocr_grid)}），跳过重建")
        return False

    # 2. 建立 OCR 列 → VLM 列映射
    ocr_header = ocr_grid[header_row_idx]
    # [自定义] 合并 OCR 表头中被拆分的单个中文字符
    # 小图片上 PaddleOCR 可能将"金额"/"税额"/"单价"等标签
    # 拆分为独立字符（如"金"+"额"），合并后便于后续列映射和标签匹配。
    ocr_header = _merge_split_header_chars(ocr_header)
    # 使用拼接行的原始单元格文本（不展开 colspan）做列匹配
    concat_row_cells = rows[data_row_start].find_all(["td", "th"])
    # 如果 data_row_start 未指向拼接行，则查找真正的拼接行
    for vi in range(max(0, data_row_start), len(rows)):
        row_texts_raw = [c.get_text().strip() for c in rows[vi].find_all(["td", "th"])]
        concat_count = 0
        for t in row_texts_raw:
            if not t:
                continue
            norm_t = _normalize_for_matching(t).replace(" ", "")
            if any(norm_t.startswith(_normalize_for_matching(kw).replace(" ", ""))
                   for kw in _INVOICE_DATA_COLUMN_KEYWORDS):
                nums = [n for n in re.findall(r'\d+\.?\d*', t) if len(n) >= 2]
                if len(nums) >= 2:
                    concat_count += 1
        if concat_count >= 2:
            concat_row_cells = rows[vi].find_all(["td", "th"])
            break
    vlm_row_texts = [c.get_text().strip() for c in concat_row_cells]
    col_map = _map_ocr_cols_to_vlm_cols(ocr_header, vlm_row_texts)

    if len(col_map) < 3:
        logger.debug(
            f"OCR 列映射不足（{len(col_map)} 列），跳过重建。"
            f"OCR表头={ocr_header[:8]}，VLM行文本={[t[:30] for t in vlm_row_texts[:8]]}"
        )
        return False

    # 3. 获取拼接行的单元格模板（保留 colspan/rowspan）
    template_cells = concat_row_cells
    if not template_cells:
        return False

    # 4. 为每个 OCR 数据行创建新 <tr>
    # OCR 网格行列数不均（空列被压缩），不依赖 col_map 索引，
    # 而是逐 OCR 单元格与 VLM 模板列做文本/类型匹配
    new_rows: list[Tag] = []
    for ocr_row in ocr_data_rows:
        new_tr = soup.new_tag("tr")
        # 记录已使用的 OCR 单元格（避免重复分配到多列）
        used_ocr: set[int] = set()
        # 预拆分队列：(ocr_cell_index, split_part_index, value) — 用于直接填充后续列
        pending_splits: list[tuple[int, int, str]] = []

        for vc in range(len(template_cells)):
            new_td = soup.new_tag("td")
            orig = template_cells[vc]
            for attr in ("colspan", "rowspan"):
                if orig.get(attr):
                    new_td[attr] = orig[attr]

            cell_text = ""
            vlm_tpl_text = orig.get_text().strip()
            vlm_tpl_norm = _normalize_for_matching(vlm_tpl_text).replace(" ", "")
            vlm_is_num_col = _is_numeric_column(vlm_tpl_text)

            # 优先使用预拆分的值（上一列拆分出的后续值）
            if pending_splits:
                _, _, split_val = pending_splits.pop(0)
                cell_text = split_val
            else:
                # 找最佳匹配的 OCR 单元格
                best_oc = -1
                for oc, ocr_cell in enumerate(ocr_row):
                    if oc in used_ocr:
                        continue
                    ocr_cell = ocr_cell.strip()
                    if not ocr_cell:
                        continue
                    ocr_norm = _normalize_for_matching(ocr_cell).replace(" ", "")

                    # 精准匹配：OCR 文本在 VLM 模板文本中（或反过来）
                    if ocr_norm and (ocr_norm in vlm_tpl_norm or vlm_tpl_norm in ocr_norm):
                        best_oc = oc
                        break

                # 若文本未匹配，用类型匹配：数值 OCR → 数值 VLM 列
                # ¥ 前缀值（如 "￥71006.01"）仅通过文本匹配分配，
                # 不通过类型匹配，以避免被先遍历到的数值列抢先占用
                if best_oc < 0 and vlm_is_num_col:
                    for oc, ocr_cell in enumerate(ocr_row):
                        if oc in used_ocr:
                            continue
                        ocr_stripped = ocr_cell.strip()
                        if not _is_data_value(ocr_stripped):
                            continue
                        if ocr_stripped.startswith('￥') or ocr_stripped.startswith('¥'):
                            continue
                        best_oc = oc
                        break

                if best_oc >= 0:
                    cell_text = ocr_row[best_oc].strip()
                    used_ocr.add(best_oc)
                    # 若该 OCR 文本含多个拼接数值且匹配的 VLM 列是数值列，
                    # 尝试拆分并预填后续列
                    if vlm_is_num_col:
                        nums_in_text = [n for n in re.findall(r'\d+\.?\d*', cell_text) if len(n) >= 2]
                        # 统计后续连续数值列数
                        next_num_cols = 0
                        for nvc in range(vc + 1, len(template_cells)):
                            nvl_text = template_cells[nvc].get_text().strip()
                            if _is_numeric_column(nvl_text):
                                next_num_cols += 1
                            else:
                                break
                        if len(nums_in_text) >= 2 and next_num_cols >= 1:
                            parts = _try_split_concatenated_numbers(cell_text, min(1 + next_num_cols, len(nums_in_text)))
                            if len(parts) > 1:
                                cell_text = parts[0]
                                for pi in range(1, len(parts)):
                                    pending_splits.append((best_oc, pi, parts[pi]))

            new_td.string = cell_text
            new_tr.append(new_td)
        new_rows.append(new_tr)

    # 提取 OCR 表头标签（供后续步骤 4.5 和步骤 5 共用）
    ocr_header_labels = [
        h for h in ocr_header
        if h.strip() in _INVOICE_HEADER_KEYWORDS
    ]

    # 4.5 修复 OCR 重建表格中合计行的 ¥ 值列位置
    # OCR 行中的合计行（首列为"合计"）经过模板列匹配后，
    # ¥ 值可能被分配到错误的列（模板列文本匹配对 ¥ 值不感知列语义）。
    # 需要按 TH 行的 colspan 结构重建合计行，将 ¥ 值对齐到正确列。
    _fix_ocr_summary_row_yen_position(
        new_rows, template_cells, ocr_header, ocr_header_labels,
        col_map, soup, ocr_data_rows,
    )

    # 4.6 值保真校验：OCR 重建不得丢失 VLM 拼接单元格中已有的数值
    # （原则 1/4/10）。若重建丢失了 VLM 数值（如数量 "15910.00"），
    # 放弃重建、保留 VLM 原始拼接输出，交由下游确定性拆分处理。
    if not _reconstruction_preserves_vlm_values(template_cells, new_rows):
        logger.warning(
            "OCR 重建丢失 VLM 拼接数值，放弃重建、保留 VLM 原始拼接行"
        )
        return False

    # 5. 在 VLM 拼接行之前插入 OCR 表头行
    # OCR 表头来自 ocr_grid[header_row_idx] 中在 _INVOICE_HEADER_KEYWORDS 里的标签
    if ocr_header_labels and len(ocr_header_labels) >= 3:
        # 用 col_map 将 OCR 表头标签映射到 VLM 列位置
        header_tr = soup.new_tag("tr")
        for vc in range(len(template_cells)):
            th = soup.new_tag("th")
            orig = template_cells[vc]
            for attr in ("colspan", "rowspan"):
                if orig.get(attr):
                    th[attr] = orig[attr]
            # 查找映射到此 VLM 列的 OCR 表头标签
            label = ""
            vlm_tpl_text = orig.get_text().strip()
            vlm_tpl_norm = _normalize_for_matching(vlm_tpl_text).replace(" ", "")
            for oc_hdr in ocr_header_labels:
                hdr_norm = _normalize_for_matching(oc_hdr).replace(" ", "")
                if hdr_norm and vlm_tpl_norm and (hdr_norm in vlm_tpl_norm or vlm_tpl_norm in hdr_norm):
                    label = oc_hdr
                    break
            # 若未匹配，用 col_map 索引
            if not label:
                for oc, mapped_vc in col_map.items():
                    if mapped_vc == vc and oc < len(ocr_header):
                        candidate = ocr_header[oc].strip()
                        if candidate in _INVOICE_HEADER_KEYWORDS:
                            label = candidate
                            break
            th.string = label
            header_tr.append(th)
        new_rows.insert(0, header_tr)
        logger.debug(f"已插入 OCR 表头行：{len(ocr_header_labels)} 个标签")

    # 6. 替换原 VLM 拼接行
    # 找出被拼接的行（含多个以发票明细列关键词开头且含≥2个数值的单元格）
    concat_row_idx = None
    for vi in range(max(0, data_row_start), len(rows)):
        row_texts = vlm_data[vi] if vi < len(vlm_data) else []
        concat_count = 0
        for t in row_texts:
            if not t:
                continue
            norm_t = _normalize_for_matching(t).replace(" ", "")
            starts_with_kw = any(
                norm_t.startswith(_normalize_for_matching(kw).replace(" ", ""))
                for kw in _INVOICE_DATA_COLUMN_KEYWORDS
            )
            if starts_with_kw:
                nums = [n for n in re.findall(r'\d+\.?\d*', t) if len(n) >= 2]
                if len(nums) >= 2:
                    concat_count += 1
        if concat_count >= 2:
            concat_row_idx = vi
            break

    if concat_row_idx is None:
        concat_row_idx = data_row_start  # 回退

    # 保存插入点（拼接行之前的那一行）
    insertion_point = rows[concat_row_idx - 1] if concat_row_idx > 0 else None

    # 删除拼接行
    rows[concat_row_idx].decompose()

    # 在插入点之后插入新行
    if insertion_point is not None:
        target = insertion_point
        for new_tr in reversed(new_rows):
            target.insert_after(new_tr)
    else:
        # 没有前一行（表格第一行），插入到表格开头
        table_tag = table
        first = table_tag.find("tr")
        if first:
            for new_tr in reversed(new_rows):
                first.insert_before(new_tr)
        else:
            for new_tr in new_rows:
                table_tag.append(new_tr)

    logger.info(
        f"OCR 网格重建表格行：将 {len(rows) - data_row_start} 行 VLM 数据行 "
        f"替换为 {len(new_rows)} 行 OCR 数据行 "
        f"（OCR 表头行={header_row_idx}，列映射={len(col_map)}）"
    )
    return True


def _fill_empty_cells_from_ocr_grid(
    soup: BeautifulSoup,
    table: Tag,
    ocr_grid: list[list[str]],
) -> bool:
    """将 OCR 网格中的文字填充到表格中的空单元格和含图片单元格。

    修复了三个关键问题：
    1. 含 <img> 的行不再被误判为表头（Bug 1）
    2. 使用标签锚点对齐 OCR 行与 VLM 行，而非简单索引映射（Bug 2）
    3. 使用 Tag.append() 追加文本，保留已有 <img> 子元素（Bug 3）

    Args:
        soup: BeautifulSoup 对象。
        table: <table> Tag。
        ocr_grid: OCR 识别文字网格。

    Returns:
        是否对表格做了任何修改。
    """
    rows = table.find_all("tr")
    if not rows:
        return False

    ocr_nrows = len(ocr_grid)
    if ocr_nrows == 0:
        return False

    # 1. 解析 VLM 表格结构
    vlm_data, vlm_cells = _parse_vlm_table_structure(rows)
    if not vlm_data:
        return False

    # 2. 检测数据行起始（含 <img> 的行终止表头扩展）
    data_row_start = _detect_data_row_start(rows, vlm_data)

    # [自定义] 检测 VLM 行合并并用 OCR 网格重建
    # 仅对发票表格执行，避免影响其他类型表格
    # 合并上游时注意：此 hook 只依赖本模块内部函数
    try:
        if _is_invoice_table(table) and _has_concatenated_data_cells(vlm_data):
            logger.info(
                f"检测到 VLM 行合并：OCR 数据行={_count_ocr_data_rows(ocr_grid)}, "
                f"VLM 数据行={len(vlm_data) - data_row_start}，"
                f"优先确定性拆分，失败则回退 OCR 网格重建"
            )
            # 原则 4：VLM 拼接值完整正确时，直接按数据行数 N 确定性拆回多行，
            # 避免 OCR 行聚类的非确定性导致丢值（如丢失 "15910.00"）。
            if _split_concatenated_row_deterministically(
                soup, table, vlm_data, data_row_start
            ):
                return True  # 拆分成功，跳过空单元格填充
            if _rebuild_merged_rows_from_ocr(
                soup, table, ocr_grid, vlm_data, vlm_cells, data_row_start
            ):
                return True  # 重建成功，跳过空单元格填充
    except Exception:
        logger.exception(
            "OCR 网格重建失败，回退到空单元格填充逻辑"
        )

    # 3. 使用标签锚点将 OCR 行对齐到 VLM 数据行
    ocr_pool = _align_ocr_to_vlm_rows(ocr_grid, vlm_data, data_row_start)

    # 4. 推断列类型
    header_types = _infer_column_types_from_header(vlm_data, vlm_cells)

    # 5. 按行填充
    modified = False
    filled_cell_ids = set()  # 记录已填充的 Tag id，处理 colspan 重复引用

    # 预计算「表头/分区标题」规范化文本集（data_row_start 之前），供跨行去重：
    # 顶部标题（如「流动资产:」）虽在另一行，也属 VLM 已含的标签，OCR 读到全角
    # 变体时不应复制进数据行空列。仅对表头行做跨行去重，数据行之间不互相去重，
    # 以保留「吨/免税」等可合法重复出现的值被放置进漏识别单元格的能力（原则 1）。
    vlm_header_norm = {
        _normalize_for_matching(t)
        for row in vlm_data[:data_row_start]
        for t in row
        if t
    }

    for vlm_row_idx in range(data_row_start, len(vlm_data)):
        vlm_row = vlm_data[vlm_row_idx]
        vlm_tag_row = vlm_cells[vlm_row_idx]

        # 收集该 VLM 行对应的 OCR 文本（过滤已在 VLM 中存在的标签）
        ocr_texts = ocr_pool.get(vlm_row_idx, [])
        ocr_new: list[tuple[str, str]] = []
        # 本行 VLM 单元格的规范化文本（去重比对用，全角/半角标点一致）
        vlm_row_norms = [_normalize_for_matching(vt) for vt in vlm_row if vt]
        for ot in ocr_texts:
            item_type = _classify_ocr_item_type(ot)
            if item_type == "text":
                ot_norm = _normalize_for_matching(ot)
                # 文本（标签）：同行或表头行已含该标签即视为重复，不做填充——
                # 只"放置"新增信息，不复制已有信息（原则 1）。
                # 仅查同行 + 表头，不查其它数据行，避免误伤可重复出现的值。
                # 去重分两级：
                # ① 规范化后精确相等（覆盖全角/半角标点差异）；
                # ② 较长文本（≥4 字）的子串匹配——OCR 常截断 VLM 标签或丢失
                #    序号前缀，如「、经营活动产生的现金流量：」是
                #    「一、经营活动产生的现金流量:」去掉「一、」的截断重复。
                #    仅对较长文本启用，避免误伤「吨」「免税」等短值。
                if ot_norm in vlm_row_norms:
                    continue
                if len(ot_norm) >= 4 and any(
                    ot_norm in vt_norm for vt_norm in vlm_row_norms
                ):
                    continue
                if ot_norm in vlm_header_norm:
                    continue
                if len(ot_norm) >= 4 and any(
                    ot_norm in ht_norm for ht_norm in vlm_header_norm
                ):
                    continue
            else:
                # 数值/税率：仅与同行精确比对去重（数值可合法重复出现）
                if any(ot == vt for vt in vlm_row if vt):
                    continue
            ocr_new.append((ot, item_type))

        if not ocr_new:
            continue

        # 找出可填充的列
        fillable = _get_fillable_columns(vlm_row, vlm_tag_row, header_types)
        if not fillable:
            continue

        # 匹配填充：优先将 OCR 文本填到含图片的单元格（append），再填纯空单元格
        # 文本类 OCR 优先填 append 单元格（如姓名），数字类 OCR 可填任意匹配类型
        # 注意：filled_cell_ids 仅对 "empty" 模式生效（防止重复填充空单元格），
        # "append" 模式允许同一 Tag 多次追加（单 colspan 行含多个 <img> 场景）
        for ocr_text, ocr_type in ocr_new:
            placed = False
            # 第一轮：文本类型 → append 模式单元格
            if ocr_type == "text":
                for fi, (vc, ct, mode) in enumerate(fillable):
                    cell_tag = vlm_tag_row[vc]
                    if mode != "append":
                        continue
                    _append_text_to_cell(cell_tag, ocr_text)
                    modified = True
                    logger.debug(
                        f"OCR 填充(append): 行{vlm_row_idx}列{vc} ← '{ocr_text}'"
                    )
                    fillable.pop(fi)
                    placed = True
                    break
            if placed:
                continue

            # 第二轮：任意类型 → 同类型可填充列（优先 empty 模式）
            for fi, (vc, ct, mode) in enumerate(fillable):
                cell_tag = vlm_tag_row[vc]
                if mode == "empty" and id(cell_tag) in filled_cell_ids:
                    continue
                if ocr_type == ct or mode == "empty":
                    _append_text_to_cell(cell_tag, ocr_text)
                    if mode == "empty":
                        filled_cell_ids.add(id(cell_tag))
                    modified = True
                    logger.debug(
                        f"OCR 填充({mode}): 行{vlm_row_idx}列{vc} ← '{ocr_text}'"
                    )
                    fillable.pop(fi)
                    placed = True
                    break
            if placed:
                continue

            # 第三轮：兜底 → 任意剩余可填充列
            for fi, (vc, ct, mode) in enumerate(fillable):
                cell_tag = vlm_tag_row[vc]
                if mode == "empty" and id(cell_tag) in filled_cell_ids:
                    continue
                _append_text_to_cell(cell_tag, ocr_text)
                if mode == "empty":
                    filled_cell_ids.add(id(cell_tag))
                modified = True
                logger.debug(
                    f"OCR 填充(fallback): 行{vlm_row_idx}列{vc} ← '{ocr_text}'"
                )
                fillable.pop(fi)
                break

    return modified


def _infer_column_types_from_header(
    vlm_data: list[list[str]],
    vlm_cells: list[list[Tag]],
) -> dict[int, str]:
    """从表头行推断每列的预期数据类型。

    通过表头中关键词匹配来确定：
    - 'number': 金额、税额、数量、单价 等数值列
    - 'rate': 税率、征收率 等百分比列
    - 'text': 项目名称、规格型号、单位 等文本列

    Args:
        vlm_data: VLM 表格的文本网格。
        vlm_cells: VLM 表格的 Tag 网格。

    Returns:
        {列索引: 'text'|'number'|'rate'} 映射。
    """
    NUMBER_KEYWORDS = {"金额", "税额", "数量", "单价", "价税合计"}
    RATE_KEYWORDS = {"税率", "征收率", "税率/征收率"}

    result = {}

    # 遍历前若干行找表头（含 <th> 的行）
    for row_idx in range(min(3, len(vlm_data))):
        vlm_row = vlm_data[row_idx]
        if not vlm_row:
            continue
        for col_idx, text in enumerate(vlm_row):
            if not text:
                continue
            if text in RATE_KEYWORDS:
                result[col_idx] = "rate"
            elif text in NUMBER_KEYWORDS:
                result[col_idx] = "number"
            elif col_idx not in result:
                result[col_idx] = "text"

    return result


def _classify_ocr_item_type(text: str) -> str:
    """判断 OCR 识别文字的语义类型。

    Args:
        text: OCR 识别的文字。

    Returns:
        'text' | 'number' | 'rate'。
    """
    text = text.strip()
    # 百分比
    if re.match(r'^[\d.]+\s*%$', text):
        return "rate"
    # 纯数字或货币
    if _is_data_value(text):
        return "number"
    return "text"


# ============================================================
# Hybrid 模式表格 OCR 调度（从 hybrid_model_output_to_middle_json.py 的 hook 调用）
# ============================================================

def supplement_vlm_table_cells_with_ocr(
    pdf_info_list: list,
    hybrid_pipeline_model,
    image_writer=None,
) -> None:
    """使用 Pipeline OCR 识别结果补充 VLM 表格 HTML 中的空单元格。

    遍历所有表格 span，对有空单元格的表格：
    1. 加载表格截图
    2. 运行 PaddleOCR 获取文字识别结果
    3. 按行列聚类 OCR 文字为网格
    4. 将 OCR 网格文字填充到 VLM HTML 对应空单元格

    [自定义] 此函数由 hybrid_model_output_to_middle_json.py 中的 hook 调用。
    上游合并时此模块仅需保留，无需修改。

    Args:
        pdf_info_list: 中间 JSON 的页面列表。
        hybrid_pipeline_model: Hybrid pipeline 模型实例（含 ocr_model）。
        image_writer: 可选的 FileBasedDataWriter，用于解析图片相对路径。
            若提供且图片加载失败，会尝试从 image_writer 的根目录读取。
    """
    import os

    import cv2
    from bs4 import BeautifulSoup
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    ocr_model = hybrid_pipeline_model.ocr_model
    filled_count = 0
    skipped_count = 0

    for page_info in pdf_info_list:
        for block in page_info.get("preproc_blocks", []):
            for span in iter_block_spans(block):
                if span.get("type") != ContentType.TABLE:
                    continue

                html = span.get("html", "")
                if not html:
                    continue

                # 检查是否有空单元格需要填充
                try:
                    soup = BeautifulSoup(html, "html.parser")
                    table = soup.find("table")
                    if not table:
                        continue
                    # 快速检查：是否存在空文本的 <td>
                    has_empty = any(
                        not cell.get_text().strip()
                        for cell in table.find_all("td")
                    )
                    # 检查是否存在含 <img> 的单元格
                    #（VLM 无法识别手写/签章文字时将其渲染为图片，
                    #   即使 get_text() 有标签文字，也需要 OCR 补充值文本）
                    has_img = any(
                        cell.find("img") is not None
                        for cell in table.find_all("td")
                    )
                    # 发票表格即使无空单元格也需 OCR，
                    # 用于检测和纠正 VLM 的多行拼接问题
                    is_invoice = _is_invoice_table(table)
                    if not has_empty and not has_img and not is_invoice:
                        continue
                    # 结构性稀疏表格（如财务报表、征信报告）的空单元格是合法留白，
                    # 非 VLM 遗漏，不应做 OCR 填充（否则会因列类型退化为全 "text" 而把
                    # 表头文字/行标签误填进空列，产生重复内容）。发票与含 <img> 的表格除外。
                    if _is_structurally_sparse_table(table) and not has_img and not is_invoice:
                        continue
                    # [自定义] 财务报表样式表格（表头含「行次」）即使非稀疏（密集报表，
                    # 大量「-」占位）其空单元格也是合法留白。OCR 行对齐错位会灌入
                    # 截断标签/合并数字/单字噪声，故跳过 OCR 补充。发票与含 <img> 表格除外。
                    if _is_financial_statement_table(table) and not has_img and not is_invoice:
                        continue
                except Exception:
                    continue

                # 获取表格图片路径并加载
                image_path = span.get("image_path", "")
                if not image_path:
                    skipped_count += 1
                    continue

                try:
                    table_img = cv2.imread(image_path)
                    if table_img is None:
                        # 图片路径可能为相对路径（如仅 hash 文件名），
                        # 尝试通过 image_writer 的根目录解析完整路径
                        if image_writer is not None and hasattr(image_writer, '_parent_dir'):
                            full_path = os.path.join(image_writer._parent_dir, image_path)
                            table_img = cv2.imread(full_path)
                    if table_img is None:
                        skipped_count += 1
                        continue
                except Exception:
                    skipped_count += 1
                    continue

                h, w = table_img.shape[:2]
                if h < 10 or w < 10:
                    skipped_count += 1
                    continue

                # 运行 PaddleOCR 获取文字
                try:
                    ocr_output = ocr_model.ocr(table_img, det=True, rec=True)
                except Exception:
                    logger.exception("表格图片 PaddleOCR 执行失败")
                    skipped_count += 1
                    continue

                if not ocr_output or not ocr_output[0]:
                    skipped_count += 1
                    continue

                ocr_results = ocr_output[0]

                # 调用表格补充函数
                try:
                    new_html = supplement_empty_table_cells(
                        html, ocr_results, w
                    )
                    if new_html != html:
                        span["html"] = new_html
                        filled_count += 1
                        logger.debug(
                            f"OCR 补充表格单元格成功："
                            f"图片={image_path.split('/')[-1]}"
                        )
                except Exception:
                    logger.exception("supplement_empty_table_cells 执行失败")
                    skipped_count += 1
                    continue

    if filled_count > 0:
        logger.info(
            f"Pipeline OCR 表格补充完成：填入了 {filled_count} 个表格的空单元格，"
            f"跳过 {skipped_count} 个表格"
        )
