"""表格类型检测门控。

发票表 / 结构稀疏表 / 财务报表的判定，以及 colspan 不一致检测。
这些判定决定后续是否启用发票专用归一化与 OCR 补充，属反向门控。"""

from bs4 import BeautifulSoup, Tag
from mineru.utils.custom.table_utils._common import (
    _is_data_value,
)


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


__all__ = [
    '_FINANCIAL_STATEMENT_MARKERS',
    '_INVOICE_DETECTION_KEYWORDS',
    '_SPARSE_EMPTY_RATIO_THRESHOLD',
    '_has_colspan_mismatch',
    '_is_financial_statement_table',
    '_is_invoice_table',
    '_is_structurally_sparse_table',
]
