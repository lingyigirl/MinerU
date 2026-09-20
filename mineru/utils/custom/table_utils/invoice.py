"""增值税发票 / VAT 专用后处理。

列跨度归一化、8 列签名收拢、合计行 ¥ 值列对齐、购买方/销售方
信息多行拆分。入口 normalize_invoice_table（阶段 B 钩子 4）等。"""

import os
import re
from bs4 import BeautifulSoup, NavigableString, Tag
from loguru import logger
from mineru.utils.custom.table_utils._common import (
    _SPLIT_SUMMARY_KEYWORDS,
    _get_cell_text,
    _is_data_value,
)
from mineru.utils.custom.table_utils.detect import (
    _has_colspan_mismatch,
    _is_invoice_table,
)


# -- 购买方/销售方信息行内多行拆分 --


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


__all__ = [
    '_INFO_LINE_BREAK_RE',
    '_VAT_INVOICE_COLUMN_SIGNATURE',
    '_VAT_INVOICE_NAME_LABELS',
    '_VAT_INVOICE_ROW_LABELS',
    '_fix_summary_row_yen_for_th_table',
    '_format_summary_row_colspan',
    '_has_significant_rowspan',
    '_infer_missing_values_in_table',
    '_is_vat_invoice_row_label',
    '_normalize_vat_invoice_columns',
    '_shrink_row_to_cols',
    'fix_summary_row_yen_position',
    'normalize_invoice_table',
    'normalize_table_colspan',
    'split_info_cell_multiline',
]
