"""表头前缀提取与 ¥ 对齐。

入口：extract_column_header_prefixes（阶段 B 钩子 3）。"""

import re
from bs4 import BeautifulSoup
from loguru import logger
from mineru.utils.custom.table_utils._common import (
    _is_data_value,
)
from mineru.utils.custom.table_utils.invoice import (
    _fix_summary_row_yen_for_th_table,
)
from mineru.utils.custom.table_utils.ocr_align import (
    _match_data_column_keyword,
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

    # 该函数还需 _fix_summary_row_yen_for_th_table（invoice）与
    # _match_data_column_keyword（ocr_align），已由模块顶部导入提供。

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


__all__ = [
    'extract_column_header_prefixes',
]
