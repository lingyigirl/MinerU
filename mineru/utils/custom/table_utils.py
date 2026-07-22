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
            # 无法分类的不明 token，跳过
            logger.debug(f"无法分类的 token: {token}，跳过")
            continue

    if not header_tokens:
        return [], None

    return header_tokens, data_tokens if data_tokens else None


def _strip_header_prefix(token: str) -> Optional[dict]:
    """检查 token 是否以已知表头关键词开头，若匹配则剥离。

    Args:
        token: 待检查的 token。

    Returns:
        {"header": 表头关键词, "data": 剩余文本} 或 None。
    """
    for header in sorted(_INVOICE_HEADER_KEYWORDS, key=len, reverse=True):
        if token.startswith(header) and len(token) > len(header):
            data = token[len(header):].strip()
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
    return re.sub(r'^[.,;:!?。，、；：！？·•\-–—]+', '', token).strip() or token


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

    # 清空"合计"原位置和中间的空列
    for i in range(1, n_cols):
        if i not in target_slots:
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
            cell.string = data_part

            # 创建合计行：复制当前行各单元格，但第一列改为合计标签
            _insert_summary_row_after(
                soup, row, matched_keyword, cells, total_columns
            )

            logger.debug(
                f"合计标签拆分（嵌入模式）：行{row_idx}列{cell_idx}，"
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

    Args:
        text: 单元格文本。

    Returns:
        匹配到的关键词字符串，或 None。
    """
    for kw in sorted(_SPLIT_SUMMARY_KEYWORDS, key=len, reverse=True):
        if text.endswith(kw):
            return kw
        # 也检查关键词前有空格分隔的情况
        if f" {kw}" in text:
            return kw
    return None


def _strip_summary_keyword(text: str, keyword: str) -> str:
    """从文本中移除末尾的摘要关键词。

    Args:
        text: 原始文本。
        keyword: 要移除的关键词。

    Returns:
        剥离后的文本。
    """
    # 精确末尾匹配
    if text.endswith(keyword):
        result = text[:-len(keyword)].rstrip()
        return result
    # 关键词前有空格
    idx = text.rfind(f" {keyword}")
    if idx >= 0:
        result = text[:idx].rstrip()
        return result
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
        elif _is_data_value(cell_val):
            # 数值列复制数据值
            td.string = cell_val
        else:
            # 非数值列留空
            td.string = ""
        summary_tr.append(td)

    data_row.insert_after(summary_tr)


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
        computed_rate = round(tax_val / amount_val * 100)
        # 仅当税率在合理范围内（0-20% 或精确匹配如 13/9/6/3）才填充
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

    # 先执行通用的 colspan 规范化
    html = normalize_table_colspan(html) if _has_colspan_mismatch(html) else html

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
            items.append((cx, cy, text.strip()))
        except (IndexError, TypeError, ValueError):
            continue

    if not items:
        return []

    # 按 y 坐标排序
    items.sort(key=lambda it: it[1])

    # 按 y 坐标相似度聚类分行
    # 同一行的文字具有接近的 y 坐标（差异 < 10 像素）
    # 不同行的文字有显著不同的 y 坐标
    Y_TOLERANCE = 10.0
    rows = []
    current_row = [items[0]]
    for item in items[1:]:
        current_avg_y = sum(it[1] for it in current_row) / len(current_row)
        if abs(item[1] - current_avg_y) <= Y_TOLERANCE:
            current_row.append(item)
        else:
            rows.append(current_row)
            current_row = [item]
    rows.append(current_row)

    # 每行内按 x 排序
    grid = []
    for row in rows:
        row.sort(key=lambda it: it[0])
        grid.append([it[2] for it in row])

    return grid


def _fill_empty_cells_from_ocr_grid(
    soup: BeautifulSoup,
    table: Tag,
    ocr_grid: list[list[str]],
) -> bool:
    """将 OCR 网格中的文字填充到表格中的空单元格。

    匹配策略：
    1. 遍历 VLM 表格所有行，收集每个 <td> 的文本
    2. 识别哪一行/列对应 OCR 网格的哪一行/列
    3. 对于每个空单元格，尝试从 OCR 网格对应位置取文字填充

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

    # 解析 VLM 表格：收集每行的单元格文本和行列信息
    vlm_data = []  # list[list[str]]  每行每列的文本
    vlm_cells = []  # list[list[Tag]]  对应的 BeautifulSoup Tag

    for row in rows:
        row_cells = row.find_all(["td", "th"])
        if not row_cells:
            continue

        row_texts = []
        row_tags = []
        for cell in row_cells:
            colspan = int(cell.get("colspan", 1))
            text = cell.get_text().strip()
            # colspan > 1 的单元格展开（重复填入多次以对齐网格）
            for _ in range(colspan):
                row_texts.append(text)
                row_tags.append(cell)
        vlm_data.append(row_texts)
        vlm_cells.append(row_tags)

    if not vlm_data:
        return False

    # 确定 VLM 表格的列数（取最大行宽）

    # 计算 OCR 网格的维度（取最大列数）
    ocr_nrows = len(ocr_grid)
    if ocr_nrows == 0:
        return False

    # 跳过表头行（第一行 + 任何包含 <th> 的行）
    data_row_start = 0
    for i, row in enumerate(rows):
        if row.find("th"):
            data_row_start = i + 1
        else:
            # 检查第一个非表头的全文本行（类表头）
            cells = row.find_all("td")
            texts = [c.get_text().strip() for c in cells]
            if texts and all(
                len(t) < 15 and not _is_data_value(t)
                for t in texts if t
            ):
                data_row_start = i + 1

    # 匹配：从表头推断列类型，按类型匹配 OCR 文字到空列
    modified = False

    # 第一步：从表头行推断每列的预期数据类型
    # ['项目名称'(text), '规格型号'(text), '单位'(text), '数量'(num), '单价'(num), '金额'(num), '税率/征收率'(rate), '税额'(num)]
    header_types = _infer_column_types_from_header(vlm_data, vlm_cells)

    for vlm_row_idx in range(data_row_start, len(vlm_data)):
        vlm_row = vlm_data[vlm_row_idx]
        vlm_tag_row = vlm_cells[vlm_row_idx]

        # 映射到 OCR 网格行
        ocr_row_idx = vlm_row_idx - data_row_start
        if ocr_row_idx >= ocr_nrows:
            break
        ocr_row = ocr_grid[ocr_row_idx]

        # 找出 VLM 行中的空列及其预期类型
        empty_columns = []  # [(col_idx, header_type)]
        for vc in range(len(vlm_row)):
            if not vlm_row[vc].strip():
                col_type = header_types.get(vc, "text")
                empty_columns.append((vc, col_type))

        if not empty_columns:
            continue

        # 将 OCR 项分类（跳过 VLM 已存在的值）
        ocr_new_items = []  # [(text, item_type)]
        for ocr_text in ocr_row:
            if not ocr_text:
                continue
            # 跳过已在 VLM 行中存在的值
            if any(ocr_text == vlm_row[vc] for vc in range(len(vlm_row))):
                continue
            item_type = _classify_ocr_item_type(ocr_text)
            ocr_new_items.append((ocr_text, item_type))

        if not ocr_new_items:
            continue

        # 按类型匹配：OCR 项 → 同类型空列
        for ocr_text, item_type in ocr_new_items:
            # 找到第一个匹配类型的空列
            for ec_idx, (vc, col_type) in enumerate(empty_columns):
                if item_type == col_type:
                    cell_tag = vlm_tag_row[vc]
                    if cell_tag.name == "th":
                        continue
                    cell_tag.string = ocr_text
                    modified = True
                    logger.debug(
                        f"OCR 填充({item_type}): 行{vlm_row_idx}列{vc} ← '{ocr_text}'"
                    )
                    empty_columns.pop(ec_idx)
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
