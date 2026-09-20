"""合并单元格拆分引擎。

处理 VLM 把一整行数据合并进单个 colspan 单元格的情况：
识别合并行、按关键词切分 token、回填到各列、必要时补表头行。
入口：split_merged_table_cells（阶段 B 钩子 1）。"""

import re
from typing import Optional
from bs4 import BeautifulSoup, Tag
from loguru import logger
from mineru.utils.custom.table_utils._common import (
    _INVOICE_HEADER_KEYWORDS,
    _compute_total_columns,
    _is_data_value,
    _is_text_token,
    _strip_header_prefix,
    _strip_leading_punctuation,
)


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


__all__ = [
    '_allocate_summary_row',
    '_allocate_to_columns',
    '_classify_tokens',
    '_fill_data_row_columns',
    '_fix_embedded_summary',
    '_fix_parenthetical_annotations',
    '_has_header_row',
    '_insert_header_row',
    '_is_fuzzy_number_match',
    '_is_merged_row',
    '_merge_split_keywords',
    '_pad_with_empty_smart',
    '_resolve_summary_target_slots',
    '_split_merged_cell_text',
    '_try_split_partial_merge',
    'split_merged_table_cells',
]
