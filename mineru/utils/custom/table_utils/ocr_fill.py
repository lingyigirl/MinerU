"""行重建与空单元格填充核心。

确定性重建被拼接的数据行，并把 OCR 结果填回 VLM 表格的空单元格。
入口：supplement_empty_table_cells（阶段 A 通用空单元格补充）。"""

import re
from bs4 import BeautifulSoup, Tag
from loguru import logger
from mineru.utils.custom.table_utils._common import (
    _INVOICE_HEADER_KEYWORDS,
    _build_column_profiles,
    _cjk_count,
    _classify_ocr_item_type,
    _compact_norm,
    _edit_distance_le1,
    _is_data_value,
    _is_lossy_row_variant,
    _is_merged_noise,
    _is_pure_punctuation,
    _is_same_row_value_variant,
    _is_single_cjk_char,
    _normalize_for_matching,
    _violates_column_contract,
)
from mineru.utils.custom.table_utils.detect import (
    _is_invoice_table,
)
from mineru.utils.custom.table_utils.ocr_guards import (
    _filter_seal_overlap_tokens,
)
from mineru.utils.custom.config import get_table_ocr_min_confidence
from mineru.utils.custom.table_utils.ocr_align import (
    _INVOICE_DATA_COLUMN_KEYWORDS,
    _NUMERIC_COLUMN_KEYWORDS,
    _align_ocr_to_vlm_rows,
    _append_text_to_cell,
    _build_ocr_text_grid,
    _count_ocr_data_rows,
    _detect_data_row_start,
    _find_ocr_header_row,
    _get_fillable_columns,
    _has_concatenated_data_cells,
    _is_numeric_column,
    _map_ocr_cols_to_vlm_cols,
    _match_data_column_keyword,
    _merge_split_header_chars,
    _parse_vlm_table_structure,
    _split_decimal_values,
    _split_integer_quantity,
    _split_name_cell,
    _try_split_concatenated_numbers,
)


# Hybrid 模式表格 OCR 补全调度


def supplement_empty_table_cells(
    vlm_html: str,
    ocr_results: list,
    table_img_width: int = 0,
    chrome: dict | None = None,
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
        chrome: 守卫 5/7 过滤上下文（可选）：
            {"seals": 文档级印章文本集合, "title": 本页标题或空串,
             "seal_overlaps": 印章-表格重叠区域(页面坐标), "table_bbox": 表格 bbox}。
            由 supplement_vlm_table_cells_with_ocr 采集下传，透传给
            _fill_empty_cells_from_ocr_grid；为 None 时守卫 5/7 不生效。

    Returns:
        补充后的 HTML 字符串；若无需补充则返回原字符串。
    """
    if not vlm_html or not ocr_results:
        return vlm_html

    chrome = chrome or {}

    # [自定义] 守卫 7-1：印章重叠区域 OCR token 过滤。
    # 印章块与表格块 bbox 重叠时，印章文字经 OCR 读入表图后会被填入空单元格。
    # 按 token 坐标过滤：若 token 中心落入印章重叠区域（页面-图片坐标变换后），
    # 判定为不属于表格数据的印章文字，从 OCR 池中删除（原则 8 不让破坏发生）。
    _overlaps = chrome.get("seal_overlaps")
    _tbbox = chrome.get("table_bbox")
    if _overlaps and _tbbox and table_img_width:
        # img_h 由调用方存入 chrome 或从此处获取（通过图片读取）
        # 若 chrome 中无 img_h，则不做高度方向过滤，仅用宽度维度
        ocr_results = _filter_seal_overlap_tokens(
            ocr_results, _overlaps, _tbbox,
            table_img_width, chrome.get("img_h", 0)
        )
        if not ocr_results:
            return vlm_html

    # === [自定义] 守卫 10：OCR 识别置信度门槛 ===
    # 低置信度识读（乱码/残影/印章叠印）宁可不填，也不污染 VLM 正确
    # 留空的单元格（原则 1 输出不多不少；宁可为空而不错填）。
    # 环境变量 MINERU_TABLE_OCR_MIN_CONFIDENCE 控制门槛（默认 0.8）；
    # 设为 "0" 可彻底关闭置信度过滤（恢复原行为）。
    _ocr_min_conf = get_table_ocr_min_confidence()
    if _ocr_min_conf > 0:
        _kept = []
        _dropped = 0
        for _r in ocr_results:
            _ti = _r[1] if len(_r) > 1 else None
            _score = _ti[1] if isinstance(_ti, (list, tuple)) and len(_ti) > 1 else None
            if _score is not None and _score < _ocr_min_conf:
                _dropped += 1
                continue
            _kept.append(_r)
        if _dropped:
            logger.debug(f"守卫 10 置信度过滤(<{_ocr_min_conf}): 丢弃 {_dropped} 个 token")
        ocr_results = _kept
        if not ocr_results:
            return vlm_html
    # === end 守卫 10 ===

    try:
        soup = BeautifulSoup(vlm_html, "html.parser")
    except Exception:
        logger.warning("BeautifulSoup 解析表格 HTML 失败，跳过 OCR 补充")
        return vlm_html

    # 构建 OCR 网格（含每行 y 中心，供 G6A 行池 y-extent 来源守卫）
    ocr_grid, ocr_row_y = _build_ocr_text_grid(ocr_results, table_img_width)
    if not ocr_grid:
        return vlm_html

    # 找到所有表格
    modified = False
    for table in soup.find_all("table"):
        try:
            modified = _fill_empty_cells_from_ocr_grid(
                soup, table, ocr_grid, chrome=chrome, ocr_row_y=ocr_row_y
            ) or modified
        except Exception:
            logger.exception("OCR 网格填充单表时出错，跳过此表格")
            continue

    if modified:
        return str(soup)
    return vlm_html


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


def _remove_orphaned_summary_continuation_row(
    cont_row: Tag,
    concat_has_rowspan: bool,
    new_rows: list[Tag],
) -> None:
    """删除 VLM 拼接行被重建后遗留的 rowspan 续行（原始合计 ¥ 行）。

    VLM 将发票「金额/税额」列的合计值放在拼接行的 rowspan 续行中
    （如 <tr><td>¥34552.25</td><td>¥1036.57</td></tr>）。重建拼接行时
    只替换拼接行本身，续行被遗留，形成孤立重复的合计 ¥ 行（违反原则 1）。

    删除条件（原则 4 强约束，全部满足才删除，否则保持原样）：
    1. 拼接行含 rowspan≥2 的单元格（续行的前提）；
    2. 续行所有非空单元格均为 ¥/￥ 数值（无其它文本）；
    3. 续行的 ¥ 数值已被重建新行保留（规范化后比对，避免丢值）。

    注意：拼接行在调用本函数前已被 decompose()，bs4 的 decompose() 会清空
    子节点，故 rowspan 信息需由调用方在 decompose 之前捕获后经
    ``concat_has_rowspan`` 传入，而非在此处读取拼接行 Tag。

    Args:
        cont_row: 待判定/删除的续行。
        concat_has_rowspan: 拼接行在 decompose 前是否含 rowspan≥2 的单元格。
        new_rows: 重建后的新行（含合计行），用于校验 ¥ 值是否已保留。
    """
    # 条件 1：拼接行须含 rowspan（否则不存在续行）
    if not concat_has_rowspan:
        return
    # 条件 2：续行须为「仅含 ¥/￥ 数值与空单元格」的合计行
    yen_values: list[str] = []
    for c in cont_row.find_all(["td", "th"]):
        text = c.get_text().strip()
        if not text:
            continue
        if not re.fullmatch(r"[¥￥][\d.,]+", text):
            return  # 含非 ¥ 文本（如「免税」「3%」）→ 非续行，不删
        yen_values.append(_normalize_for_matching(text).replace(",", ""))
    if not yen_values:
        return  # 纯空行，保守不删
    # 条件 3：续行 ¥ 值须已被重建新行保留（否则删除会丢值，违反原则 1）
    rebuilt_text = _normalize_for_matching(
        "".join(c.get_text() for r in new_rows for c in r.find_all(["td", "th"]))
    ).replace(",", "")
    if any(v not in rebuilt_text for v in yen_values):
        return  # 值未保留（如 OCR 漏识别合计行），不删
    cont_row.decompose()


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
    # [自定义] 整数数量（无小数点）待反推的 (列索引, 纯数字串)。数量列在单价/金额列
    # 之前被遍历，无法当场反推，故先延迟，待步骤 4.25 单价/金额拆分完成后处理。
    deferred_quantity: tuple[int, str] | None = None
    for vc, kw, data in parsed:
        if kw == "数量":
            vals = re.findall(r"\d+\.\d{2}", data)
            if len(vals) == n_data:
                col_values[vc] = vals
            else:
                # [自定义] 整数数量（无小数点，如 "3332"+"19197"→"333219197"）无法按
                # 小数位数切分。延迟到单价/金额拆分后，用「金额 = 数量 × 单价」反推
                # （原则 4：金额/单价两列可独立可靠拆分，反推 + 双重校验必然正确）。
                digits = re.sub(r"[^\d]", "", data)
                if digits and "." not in data and len(digits) >= n_data:
                    deferred_quantity = (vc, digits)
                    col_values[vc] = [""] * n_data  # 占位，4.25 步反推后覆盖
                else:
                    return False
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
            elif 0 < len(data) < n_data and not any(ch.isdigit() for ch in data):
                # [自定义] VLM 将多行相同单位合并为单个（如单位 "吨" 而非 "吨吨"），
                # 复制到每行（原则 4：单位是列级属性，非数值且长度 < n_data 时
                # 必然是「相同单位被折叠」，复制必然正确）。
                col_values[vc] = [data] * n_data
            else:
                col_values[vc] = [""] * n_data
        else:
            # 规格型号等：置空（发票常无此列值）
            col_values[vc] = [""] * n_data

    # 4.25 [自定义] 反推整数数量（金额 = 数量 × 单价）
    if deferred_quantity is not None:
        vc_q, digits = deferred_quantity
        price_vc = next((v for v, k, _ in parsed if k == "单价"), -1)
        amount_vc = next((v for v, k, _ in parsed if k == "金额"), -1)
        quantities = _split_integer_quantity(
            digits,
            col_values.get(price_vc, []),
            col_values.get(amount_vc, []),
        )
        if len(quantities) != n_data:
            return False
        col_values[vc_q] = quantities

    # 4.5 [自定义] 从 rowspan 续行回填金额/税额合计 ¥ 值（原则 1 不多不少）
    # VLM 用 rowspan 布局表示货物区时，拼接行的金额/税额单元格无 ¥，合计放在
    # 紧随其后的续行（如 <tr><td>¥34552.25</td><td>¥1036.57</td></tr>）。
    # 仅在续行是「纯 ¥ 数值」且 ¥ 数量等于金额/税额列数时回填，否则保持原样。
    cont_row = rows[concat_row_idx + 1] if concat_row_idx + 1 < len(rows) else None
    if cont_row is not None:
        cont_nonempty = [
            c.get_text().strip()
            for c in cont_row.find_all(["td", "th"])
            if c.get_text().strip()
        ]
        if cont_nonempty and all(
            re.fullmatch(r"[¥￥][\d.,]+", t) for t in cont_nonempty
        ):
            yen_cols = [vc for vc, kw, _ in parsed if kw in ("金额", "税额")]
            if len(cont_nonempty) == len(yen_cols):
                for vc, y in zip(yen_cols, cont_nonempty):
                    if not summary_values.get(vc):
                        summary_values[vc] = y

    # 5. 构建 N 个数据行 + 1 个合计行（沿用模板列 colspan）
    # [自定义] 只复制 colspan、丢弃 rowspan：rowspan 是 VLM 拼接行「自身 + 续行
    # 占两视觉行」的标记，这里已把拼接行与续行拆成 N 个独立物理行，续行 ¥ 也已在
    # 步骤 4.5 回填并删除，故新行不应再携带 rowspan（否则渲染成 rowspan=N 的坏表）。
    new_rows: list[Tag] = []
    for i in range(n_data):
        tr = soup.new_tag("tr")
        for vc, orig in enumerate(template_cells):
            td = soup.new_tag("td")
            for attr in ("colspan",):
                if orig.get(attr):
                    td[attr] = orig[attr]
            td.string = col_values.get(vc, [""] * n_data)[i]
            tr.append(td)
        new_rows.append(tr)

    summary_tr = soup.new_tag("tr")
    for vc, orig in enumerate(template_cells):
        td = soup.new_tag("td")
        for attr in ("colspan",):
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

    # 6. 插入表头行（用拼接行模板列 + 关键词标签，保留 colspan、丢弃 rowspan）
    header_tr = soup.new_tag("tr")
    for vc, orig in enumerate(template_cells):
        th = soup.new_tag("th")
        for attr in ("colspan",):
            if orig.get(attr):
                th[attr] = orig[attr]
        kw = parsed[vc][1]
        th.string = kw if kw else orig.get_text().strip()
        header_tr.append(th)
    new_rows.insert(0, header_tr)

    # 7. 替换原拼接行
    prev = rows[concat_row_idx - 1] if concat_row_idx > 0 else None
    concat_row = rows[concat_row_idx]
    # [自定义] 捕获 rowspan（bs4 的 decompose() 会清空子节点，须在其之前读取）
    concat_has_rowspan = any(
        int(c.get("rowspan", 1)) >= 2
        for c in concat_row.find_all(["td", "th"])
    )
    concat_row.decompose()
    # [自定义] 删除拼接行的 rowspan 续行（原始合计 ¥ 行），
    # 避免重建后残留孤立重复行（如 <tr><td>¥34552.25</td><td>¥1036.57</td></tr>）。
    # 复用 OCR 重建路径的 _remove_orphaned_summary_continuation_row：三条件
    # （含 rowspan + 续行纯 ¥ + 值已被新合计行保留）全满足才删，否则保持原样。
    if concat_row_idx + 1 < len(rows):
        _remove_orphaned_summary_continuation_row(
            rows[concat_row_idx + 1], concat_has_rowspan, new_rows
        )
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
    concat_row = rows[concat_row_idx]
    # [自定义] 删除拼接行的 rowspan 续行（VLM 原始合计 ¥ 行），
    # 避免重建后残留孤立重复行（如 <tr><td>¥34552.25</td><td>¥1036.57</td></tr>）。
    # 仅当续行是纯 ¥ 值且该值已被新合计行保留时删除（原则 1 不多不少）。
    # 注意：bs4 的 decompose() 会清空子节点，须在其之前捕获 rowspan 信息。
    concat_has_rowspan = any(
        int(c.get("rowspan", 1)) >= 2
        for c in concat_row.find_all(["td", "th"])
    )
    concat_row.decompose()
    if concat_row_idx + 1 < len(rows):
        _remove_orphaned_summary_continuation_row(
            rows[concat_row_idx + 1], concat_has_rowspan, new_rows
        )

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
    chrome: dict | None = None,
    ocr_row_y: list[float] | None = None,
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
        chrome: 守卫 5 语义源过滤上下文（可选）：
            {"seals": 文档级印章文本集合, "title": 本页标题或空串}。
            为 None 时守卫 5 不生效（保持既有调用兼容）。
        ocr_row_y: 每行 OCR 的 y 中心（像素，截图坐标系），与 ocr_grid 等长；
            由 _build_ocr_text_grid 返回。为 None 时 G6A 行池 y-extent 来源
            守卫不生效（保持既有调用兼容）。

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
    ocr_pool = _align_ocr_to_vlm_rows(
        ocr_grid, vlm_data, data_row_start, ocr_row_y=ocr_row_y
    )

    # 4. 推断列类型
    header_types = _infer_column_types_from_header(vlm_data, vlm_cells)

    # 4.1 [自定义] 守卫 12：按列统计 VLM 已确立值的形态画像。
    # 与列类型解耦——类型推断不可靠（新发流水实测整表 11 列全被判为 text，
    # number 列数为 0），故判据取自「该列已有值长什么样」。
    col_profiles = _build_column_profiles(vlm_data)
    logger.debug(
        "守卫 12 列形态画像: "
        + ", ".join(
            f"列{vc}(n={p['n']},金额={p['amount_ratio']:.2f},"
            f"定长={p['is_fixed_len']},主流长={p['dom_len']})"
            for vc, p in sorted(col_profiles.items())
        )
    )

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

    # 守卫 5：本表数据区全部非空单元格的紧凑规范化文本集（供跨行截断比对）。
    # 仅当 chrome 启用时计算（nil 时构造空集，避免无谓开销）。
    # VLM 可能把相邻两格（如 对方户名+摘要）合并进同一 td 并以空白分隔
    # （`滕州英华高级中学有限公司 业务专用章`，p0 实测）——拆段各自入集，
    # 否则截断 fragment 会被「后缀值」从前后缀位置挤到中间而漏拦（guard 5B MISS）。
    if chrome:
        _cell_segs = re.compile(r"[\s　]+")
        table_cell_norms = {
            _compact_norm(seg)
            for row in vlm_data[data_row_start:]
            for t in row
            if t
            for seg in _cell_segs.split(t.strip())
            if len(_compact_norm(seg)) >= 4 and _cjk_count(seg) >= 4
        }
    else:
        table_cell_norms = set()

    # [自定义] 守卫 6 候选集：同表全部非空单元格的紧凑规范化文本
    # （含表头行 0，len≥2 且 CJK≥2）。与守卫 5 的表头无关、与列语义无关，
    # 用于跨行比对：
    #   G6B1 token 是某候选的前/后缀截断（长度差 =2）——命中「金额」表头
    #       残片（p26/p40/p44，逃脱 guard 5B 的 len≥5 下限）等短截断；
    #   G6B2 token 与某候选编辑距离 ≤1 且不比候选长（等长或更长方向，
    #       排除纯截断 prefix/suffix 与精确重复——diff=1 截断为原则 1
    #       保护的合法短值，归 G6B1(diff=2)/guard5B(len≥5) 管辖：OCR
    #       形近字重写 滕→腾/腾→滕，如 山东腾建投资集团有限公司 ← 山东
    #       滕建投资集团有限公司、往米款 ← 往来款），len≥2。
    if chrome:
        _all_table_cell_norms = {
            _compact_norm(t)
            for row in vlm_data
            for t in row
            if t and len(_compact_norm(t)) >= 2 and _cjk_count(t) >= 2
        }
    else:
        _all_table_cell_norms = set()

    def _place_checked(row_idx: int, vc: int, mode: str, text: str) -> bool:
        """守卫 12 校验通过后写入目标单元格；返回是否写入成功。

        守卫必须挂在「放置」处而非「候选」处：判定需要**目标列**身份
        （fillable 给出的 vc），而候选串的来源列身份已在池化时丢失
        （_align_ocr_to_vlm_rows 返回 dict[int, list[str]]，只留裸字符串）。
        三条放置路径（第一轮 append / 第二轮同类型 / 第三轮兜底）统一经此
        函数写入，避免守卫逻辑在三处漂移——三轮各有独立的
        _append_text_to_cell 调用点，不存在单一 choke point。
        """
        if _violates_column_contract(text, vc, col_profiles):
            logger.debug(
                f"守卫 12 拒绝(列形态契约): 行{row_idx}列{vc} ← '{text}'"
            )
            return False
        cell = vlm_cells[row_idx][vc]
        _append_text_to_cell(cell, text)
        if mode == "empty":
            filled_cell_ids.add(id(cell))
        return True

    for vlm_row_idx in range(data_row_start, len(vlm_data)):
        vlm_row = vlm_data[vlm_row_idx]
        vlm_tag_row = vlm_cells[vlm_row_idx]

        # 收集该 VLM 行对应的 OCR 文本（过滤已在 VLM 中存在的标签）
        ocr_texts = ocr_pool.get(vlm_row_idx, [])
        ocr_new: list[tuple[str, str]] = []
        # 本行 VLM 单元格的规范化文本（去重比对用，全角/半角标点一致）
        vlm_row_norms = [_normalize_for_matching(vt) for vt in vlm_row if vt]
        for ot in ocr_texts:
            # 守卫 3-①：纯标点/符号噪声（如「。」）无任何信息量，直接丢弃，
            # 不进入填充（不把噪声写进空列，输出不多不少）。
            if _is_pure_punctuation(ot):
                continue
            # 守卫 4：拼接噪音——OCR 横向合并相邻单元格成无分隔串
            # （"01-06收" = 日期 01-06 + 对公收费截断"收"；
            #  "2025-02-1416:53:28" = 交易日期 + 时间丢失空格）。
            # 这些模式在正常表格中无合法出现场景（日期/时间在各自列内
            # 不会与其它列文字粘连），判定为拼接噪音直接丢弃，不落入空列。
            if _is_merged_noise(ot):
                continue
            # 守卫 5：语义源 / 跨行截断过滤。
            # 守卫 1-4 均为内容模式守卫，但银行流水对公收费行直接空白（VLM 正确
            # 输出 <td></td>），OCR 行池 Y 聚类错位会把「表格外语义源」的 token
            # 灌入空列——这些 token 与合法公司名无内容差异，只能按来源拦截：
            #   5A-3   单 CJK ∈ 本页标题（如 p3「中」←「中国工商银行对公客户账务明细」）
            #   5A-1   文本被文档级印章行包含（如 p1「业务专用章」——印章第 3 行）
            #   5A-edit 文本与某印章行编辑距离 ≤1（如 p4「本庄三八支行」— OCR 把
            #           印章第 2 行「枣庄三八支行」的「枣」识成「本」）
            #   5B     文本是同表另一非空单元格的前/后缀截断（长度差 ≤2、
            #           token≥5 字且 CJK≥4），如 p0「州英华高级中学有限公司」←
            #          「滕州英华高级中学有限公司」截首字。
            # 审计（GT 52 页 9962 非空 cell）：此规则集 0 误伤。
            ot_cn = _compact_norm(ot)
            if chrome and ot_cn:
                _seals = chrome.get("seals") or set()
                _title = chrome.get("title") or ""
                # 5A-3：单 CJK ∈ 本页标题
                if (
                    len(ot_cn) == 1
                    and _cjk_count(ot_cn) == 1
                    and _title
                    and ot_cn in _title
                ):
                    continue
                if len(ot_cn) >= 2 and _cjk_count(ot_cn) >= 4:
                    # 5A-1 / 5A-edit：印章包含或编辑距离 ≤1
                    if any(
                        ot_cn in _s or _edit_distance_le1(ot_cn, _s)
                        for _s in _seals
                    ):
                        continue
                    # 5B：同表前后缀截断（长度差 ≤2）
                    if (
                        len(ot_cn) >= 5
                        and any(
                            len(vn) > len(ot_cn)
                            and len(vn) - len(ot_cn) <= 2
                            and (vn.startswith(ot_cn) or vn.endswith(ot_cn))
                            for vn in table_cell_norms
                        )
                    ):
                        continue
                # [自定义] 守卫 6：同表形近/宽松截断来源过滤。
                # 守卫 5 的 len>=4/len>=5 门槛对 2-4 字短 token 存在盲区——
                # 「金额」表头残片、2 字截断、形近字重写（滕→腾）等短 token
                # 仍会灌入空列。守卫 6 基于是「同表已出现过的值」的变体判定：
                #   G6B1 宽松截断：token 是某候选的前/后缀且长度差 =2
                #         （len≥2 且 CJK≥2；不受 5B 的 len≥5 下限约束）。
                #         候选集含表头行 0：如「金额」是「转出金额/转入金额」
                #         表头值的 2 字后缀截断（p26/p40/p44 实测）——这类
                #         表头残片因短于此前的 len≥5 门槛而逃脱 guard 5B；
                #   G6B2 编辑距离 ≤1 且 token 不比候选长（等长或更长方向，
                #         len≥2）：OCR 形近字重写 滕→腾/腾→滕、往→住 等，
                #         如同表真实值「山东滕建投资集团有限公司」的 OCR 变体
                #         「山东腾建投资集团有限公司」。
                # 审计（GT 52 页模型真值 vs middle 逐格）：两者并集净清除 35 个
                # WRONG/UNKNOWN 污染格，0 个真实召回误伤——3 个疑似 REAL 命中
                # 经核验均系同行 对方单位 列已有的真实滕值（同表 ED1 副本）。
                if len(ot_cn) >= 2 and _cjk_count(ot_cn) >= 2:
                    # G6B1：宽松前后缀截断（长度差 =2，短 token 版）。
                    # diff=1 排除：短 token 独占一字的截断（如「付材料款」←
                    # 「支付材料款」）是真实短值保护对象（原则 1），非表头残片。
                    # 审计（GT 52 页）：G6B1 仅命中 3 例「金额」（表头「转出金额/
                    # 转入金额」2 字后缀，diff=2），0 例 diff=1 或 diff>2。
                    # 首尾丢 2 字的高频特征（金额/凭证/摘要 等 2 字后缀截断）
                    # 使 diff=2 足够覆盖已知盲区。
                    if any(
                        len(vn) > len(ot_cn)
                        and len(vn) - len(ot_cn) == 2
                        and (vn.startswith(ot_cn) or vn.endswith(ot_cn))
                        for vn in _all_table_cell_norms
                    ):
                        continue
                    # G6B2：编辑距离 ≤1 形近（等长或更长方向）。
                    # 排除与候选完全相等的精确重复——跨行合法重复值（如「吨」）
                    # 按原则 1 保留，不在此清空；只拦"有实际编辑"的形近变体。
                    # 另外排除纯截断关系（vn.startswith(ot_cn) or
                    # vn.endswith(ot_cn)）——纯截断由 G6B1（diff=2）或
                    # guard 5B（len≥5, diff≤2）管控。diff=1 截断为合法短值
                    # 保护对象（原则 1，如「付材料款」←「支付材料款」），
                    # 不在此清空；形近字替换不产生 prefix/suffix 关系
                    # （同长或内位置换），不受此条排除。
                    if any(
                        len(vn) >= len(ot_cn)
                        and vn != ot_cn
                        and not (vn.startswith(ot_cn) or vn.endswith(ot_cn))
                        and _edit_distance_le1(ot_cn, vn)
                        for vn in _all_table_cell_norms
                    ):
                        continue
            # [自定义] 守卫 8：与同行的归一化变体比对去重。
            # 数值可合法重复出现（跨行），但**同行**内的 OCR 变体不是新信息。
            # [Fix] 提到类型分支之外无条件执行：守卫 8 原只写在 else
            # （number/rate）分支，而判型以 _classify_ocr_item_type 为准——一旦
            # 数值串被误判为 text（如全角逗号 "4，256.781.38" 不在 _is_data_value
            # 的半角字符类内），数值去重即被整条跳过。实测该串 digits 与同行
            # "4,256,781.38" 相等，守卫 8 本可直接丢弃。对无数字的纯 CJK 短值
            # 此判定天然不命中（_digits_only 为空时退化为精确相等比对），
            # 不误伤「吨/免税」等合法短值。
            if _is_same_row_value_variant(ot, vlm_row):
                continue
            # [自定义] 守卫 13：同行有损子序列（残片完整度）。
            # 守卫 8 只认「数字串完全相等」的变体，守卫 5B/G6B1/G6B2 只认
            # 「前/后缀截断（长度差 ≤2）」或「编辑距离 ≤1」，而残片的实际
            # 关系是「子序列 + 3~8 个删除」——实测滕悦 37 处 CJK 残片全部
            # 逃逸（「山东汇智慧营销策划有限」← 同行「山东汇智慧赢营销策划
            # 有限公司」，丢 3 字，非前后缀）。此处拦「同一行的值被有损重读
            # 后当作新值」。安全性来自「同行」约束：跨行同名值不受影响，
            # 四份文档实测对 VLM 原生值零误判。数字侧不在此列（同行「余额
            # 14,300,000.00」↔「转入金额 4,300,000.00」是合法的有损子序列
            # 关系，需另行设计判别，见守卫 13 的常量注释）。
            if _is_lossy_row_variant(ot, vlm_row):
                logger.debug(f"守卫 13 拒绝(同行残片): 行{vlm_row_idx} ← '{ot}'")
                continue
            # [Fix] 判型前先做全角→半角归一：OCR 的全角逗号/全角数字落在
            # _is_data_value 的半角字符类盲区，会把数值串判成 text——既拿到
            # text 列的入场券，又旁路了数值专用的守卫 8/守卫 2。归一后判型与
            # VLM 的半角形态一致（"4，256.781.38" → number）。
            item_type = _classify_ocr_item_type(_normalize_for_matching(ot))
            if item_type == "text":
                ot_norm = _normalize_for_matching(ot)
                # 守卫 3-②：单个 CJK 字符若被任一表头 token 包含
                # （如「类」∈「业务产品种类」），判为表头文字截断噪声丢弃，
                # 不落入数据行空列。
                if _is_single_cjk_char(ot_norm) and any(
                    ot_norm in ht_norm for ht_norm in vlm_header_norm
                ):
                    continue
                # 文本（标签）：同行或表头行已含该标签即视为重复，不做填充——
                # 只"放置"新增信息，不复制已有信息（原则 1）。
                # 仅查同行 + 表头，不查其它数据行，避免误伤可重复出现的值。
                # 去重分两级：
                # ① 规范化后精确相等（覆盖全角/半角标点差异）；
                # ② 较长文本（≥3 字）的子串匹配——OCR 常截断 VLM 标签或丢失
                #    序号前缀，如「、经营活动产生的现金流量：」是
                #    「一、经营活动产生的现金流量:」去掉「一、」的截断重复；
                #    或同行摘要后缀 fragment（「手续费」⊂「跨行汇款手续费」，
                #    p0 对公收费行空对方户名被其污染）。
                #    3 字门槛低于「吨/免税」等 1-2 字真实短值，不误伤。
                if ot_norm in vlm_row_norms:
                    continue
                # === [自定义] 守卫 9：短 CJK token（<3 字）是同行某单元格的前/后缀 ===
                # → 同行碎片，丢弃。用前后缀而非任意子串：避免误伤 吨/个 等
                # 被更长文本（如 2吨钢材）包含的合法短值。
                if len(ot_norm) < 3 and any(
                    len(vt_norm) >= 2
                    and (vt_norm.startswith(ot_norm) or vt_norm.endswith(ot_norm))
                    for vt_norm in vlm_row_norms
                ):
                    continue
                # === end 守卫 9 ===
                if len(ot_norm) >= 3 and any(
                    ot_norm in vt_norm for vt_norm in vlm_row_norms
                ):
                    continue
                # 反向子串去重：OCR 跨格合并（如「01-02对公收费」读成单框）时，
                # OCR token 是「日期 + 业务种类」两格值拼接的超集。此时 OCR 文本
                # 内含同单元格的值（vt_norm ⊂ ot_norm），判为重复丢弃——不把相邻
                # 格已输出的内容再复制进空列（输出不多不少）。双侧长度 ≥4 门，
                # 避免「吨/免税」等短值被长 OCR 文本误吸收。
                if len(ot_norm) >= 4 and any(
                    len(vt_norm) >= 4 and vt_norm in ot_norm
                    for vt_norm in vlm_row_norms
                ):
                    continue
                if ot_norm in vlm_header_norm:
                    continue
                if len(ot_norm) >= 4 and any(
                    ot_norm in ht_norm for ht_norm in vlm_header_norm
                ):
                    continue
                # 表头截断超集：OCR 将表头文字与相邻值/多格表头拼接成超集
                # （如「种类对公收费」），同样判为重复丢弃。
                if len(ot_norm) >= 4 and any(
                    len(ht_norm) >= 4 and ht_norm in ot_norm
                    for ht_norm in vlm_header_norm
                ):
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
                    if mode != "append":
                        continue
                    if not _place_checked(vlm_row_idx, vc, mode, ocr_text):
                        continue  # 守卫 12 形态契约不合 → 试下一列
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
            # 守卫 2：不再用 mode=="empty" 通配——空单元格只接受与列类型一致
            # 的 OCR token。number/rate 类 与 表头推断出的 text 类空列不匹配，
            # 防止 OCR 数值噪声（如 "0.0"、"106,270.070005800001"）灌入
            # 「对方户名/摘要」等文本空列（输出不多不少）。
            for fi, (vc, ct, mode) in enumerate(fillable):
                if mode == "empty" and id(vlm_tag_row[vc]) in filled_cell_ids:
                    continue
                if ocr_type == ct:
                    if not _place_checked(vlm_row_idx, vc, mode, ocr_text):
                        continue  # 守卫 12 形态契约不合 → 试下一列
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
            # 守卫 2-兜底：number 类 OCR token 不得落入表头推断为 text 的列；
            # text 类 OCR token（印章碎片/行标签截断）不得落入 number/rate 列，
            # 防止列类型修复后（如"转出金额"→number）的 name→number 跨类型污染。
            for fi, (vc, ct, mode) in enumerate(fillable):
                # [Fix] 类型门槛移出 if mode == "empty"：append 模式此前完全不过
                # 类型判定——实测 1,000,00 正是由此落进「对方户名」text 列的
                # <img> 单元格（第三轮 + append + 零门槛）。现对两种模式一律
                # 生效；第一轮已专门承担 text→append 的合法语义（如姓名追加到
                # 含图单元格），此处是第三轮兜底，不得再跨类型放行。
                if ct == "text" and ocr_type == "number":
                    continue
                if ct in ("number", "rate") and ocr_type == "text":
                    continue
                if mode == "empty" and id(vlm_tag_row[vc]) in filled_cell_ids:
                    continue
                if not _place_checked(vlm_row_idx, vc, mode, ocr_text):
                    continue  # 守卫 12 形态契约不合 → 试下一列
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
            # [自定义] 子串匹配：银行流水表头如"转出金额"/"转入金额"含"金额"子串，
            # 精确匹配会判为 text 而非 number，导致 guard 3-② 类型跳转失效，
            # seal/text 碎片被允许填入数值列（原则 1 多出）。
            if any(kw in text for kw in RATE_KEYWORDS):
                result[col_idx] = "rate"
            elif any(kw in text for kw in NUMBER_KEYWORDS):
                result[col_idx] = "number"
            elif col_idx not in result:
                result[col_idx] = "text"

    return result


__all__ = [
    '_fill_empty_cells_from_ocr_grid',
    '_fix_ocr_summary_row_yen_position',
    '_infer_column_types_from_header',
    '_rebuild_merged_rows_from_ocr',
    '_reconstruction_preserves_vlm_values',
    '_remove_orphaned_summary_continuation_row',
    '_split_concatenated_row_deterministically',
    'supplement_empty_table_cells',
]
