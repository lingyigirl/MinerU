"""VLM 表格 HTML 后处理工具。

针对 VLM 模型（MinerU2.5-Pro）在 hybrid 后端生成的表格 HTML 中，
数据行被错误地合并为单个 colspan 单元格的问题（如增值税发票中的
"项目名称 规格型号 ... 税额 数据 数据 ..."），进行自动检测和拆分。

上游合并时此模块仅需保留，无需修改。
"""

import re
from typing import Optional

from bs4 import BeautifulSoup, Tag
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
    # 延迟导入避免循环依赖（table_ocr_grid 模块级导入 table_utils）

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


# ============================================================================
# 向后兼容导出（拆分后保持旧导入路径可用）
# ============================================================================

from mineru.utils.custom.table_invoice import (  # noqa: F401
    normalize_table_colspan,
    normalize_invoice_table,
    fix_summary_row_yen_position,
    split_info_cell_multiline,
    _normalize_vat_invoice_columns,
    _format_summary_row_colspan,
    _infer_missing_values_in_table,
    _has_significant_rowspan,
    _is_vat_invoice_row_label,
    _shrink_row_to_cols,
    _fix_summary_row_yen_for_th_table,
    _VAT_INVOICE_NAME_LABELS,
    _VAT_INVOICE_COLUMN_SIGNATURE,
    _VAT_INVOICE_ROW_LABELS,
    _INFO_LINE_BREAK_RE,
)

from mineru.utils.custom.table_ocr_grid import (  # noqa: F401
    _build_ocr_text_grid,
    _parse_vlm_table_structure,
    _detect_data_row_start,
    _normalize_for_matching,
    _align_ocr_to_vlm_rows,
    _fill_empty_cells_from_ocr_grid,
    _rebuild_merged_rows_from_ocr,
    _split_concatenated_row_deterministically,
    _split_integer_quantity,
    _is_pure_punctuation,
    _is_single_cjk_char,
    _is_merged_noise,
    _compact_norm,
    _cjk_count,
    _edit_distance_le1,
    _is_ghost_table,
    _is_seal_like_text,
    _digits_only,
    _is_same_row_value_variant,
    _filter_seal_overlap_tokens,
    supplement_empty_table_cells,
    _append_text_to_cell,
    _get_fillable_columns,
    _count_ocr_data_rows,
    _match_data_column_keyword,
    _INVOICE_DATA_COLUMN_KEYWORDS,
    _is_numeric_column,
    _try_split_concatenated_numbers,
    _split_decimal_values,
    _split_name_cell,
    _has_concatenated_data_cells,
    _find_ocr_header_row,
    _merge_split_header_chars,
    _map_ocr_cols_to_vlm_cols,
    _fix_ocr_summary_row_yen_position,
    _reconstruction_preserves_vlm_values,
    _remove_orphaned_summary_continuation_row,
    _infer_column_types_from_header,
    _classify_ocr_item_type,
    _NUMERIC_COLUMN_KEYWORDS,
    _CJK_RE,
    _SEAL_NOISE_CHARS,
)

from mineru.utils.custom.table_ocr_supplement import (  # noqa: F401
    supplement_vlm_table_cells_with_ocr,
    _collect_doc_seals,
    _collect_page_title,
    _pick_title_span,
    _normalize_header_text_across_pages,
)


