"""合计/小计摘要行拆分。

处理数据行内嵌「合计/小计/总计」标签的情况：拆出摘要行、
处理 rowspan 与孤立摘要单元格、回填数值列。
入口：split_summary_from_data_cell（阶段 B 钩子 2）。"""

import re
from typing import Optional
from bs4 import BeautifulSoup, Tag
from loguru import logger
from mineru.utils.custom.table_utils._common import (
    _INVOICE_HEADER_KEYWORDS,
    _SPLIT_SUMMARY_KEYWORDS,
    _compute_total_columns,
    _get_cell_text,
    _is_data_value,
    _strip_header_prefix,
)
from mineru.utils.custom.table_utils.detect import (
    _is_invoice_table,
)


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


__all__ = [
    '_extract_number_cells',
    '_find_isolated_summary_cell',
    '_find_trailing_summary_keyword',
    '_handle_summary_split_with_rowspan',
    '_insert_summary_row_after',
    '_next_row_contains_numeric_data',
    '_split_summary_rows_in_table',
    '_strip_summary_keyword',
    'split_summary_from_data_cell',
]
