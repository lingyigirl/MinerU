"""OCR 网格构建、行对齐与列映射。

把 OCR 结果整理成文本网格，与 VLM 表格行对齐，建立 OCR 列到
VLM 列的映射，并提供拼接数值的切分工具。"""

import re
from bs4 import NavigableString, Tag
from mineru.utils.custom.table_utils._common import (
    _INVOICE_HEADER_KEYWORDS,
    _classify_ocr_item_type,
    _is_data_value,
    _normalize_for_matching,
)


def _build_ocr_text_grid(
    ocr_results: list,
    table_img_width: int = 0,
) -> tuple[list[list[str]], list[float]]:
    """将 PaddleOCR 结果按行列聚类为二维文字网格。

    步骤：
    1. 提取每个 OCR 项的文本和 bbox 中心点
    2. 按 y 坐标聚类分行（使用相邻文字行的 y 间距中位数作为聚类阈值）
    3. 每行内按 x 坐标排序

    Args:
        ocr_results: PaddleOCR 的 det+rec 输出。
        table_img_width: 表格图片宽度（像素），用于估算列聚类半径。

    Returns:
        (grid, row_y_centers)：
        - grid：二维文字网格 list[list[str]]，grid[row][col] = 文字。
        - row_y_centers：每行的 y 中心点（像素，截图坐标系），长度与 grid 相同。
          供 G6A 行池 y-extent 来源守卫判断行是否位于锚定数据带内。
    """
    if not ocr_results:
        return [], []

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
        return [], []

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
    row_y_centers = []
    for row in rows:
        row.sort(key=lambda it: it[0])
        grid.append([it[3] for it in row])
        row_y_centers.append(sum(it[1] for it in row) / len(row))

    return grid, row_y_centers


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


def _align_ocr_to_vlm_rows(
    ocr_grid: list[list[str]],
    vlm_data: list[list[str]],
    data_row_start: int,
    ocr_row_y: list[float] | None = None,
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
        ocr_row_y: 每行 OCR 的 y 中心（像素，截图坐标系），与 ocr_grid 等长；
            由 _build_ocr_text_grid 返回。为 None 时 G6A 行池 y-extent 来源
            守卫不生效（保持既有调用兼容）。

    Returns:
        {vlm_row_idx: [ocr_text, ...]} 映射。
    """
    from mineru.utils.custom.matcher import text_similarity

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

    # [自定义] G6A 行池 y-extent 来源守卫：
    # 表格截图可能包含表格区域外的文字（印章/页标题/相邻行噪声）。这些行
    # 无标签锚点（best_row 为 None），仅因"跟随上一个锚定行"而落入空行空列
    # 池——即已知泄漏（业务专用章/中/州英华…）的行池成因。此处先预计算每行
    # 是否锚定成功，收集锚定行的 y 中心形成数据带 y-extent；对从未锚定且
    # y 中心落在数据带之外的行直接丢弃，不进入行池。
    row_anchor: list[int | None] = []
    anchored_y: list[float] = []
    for oi, ocr_row in enumerate(ocr_grid):
        best_row = None
        best_score = 0
        for vi in range(data_row_start, vlm_nrows):
            score = 0
            for ot in ocr_row:
                if not ot:
                    continue
                ot_norm = _normalize_for_matching(ot)
                for vt_norm in vlm_norm[vi]:
                    if not vt_norm:
                        continue
                    # 子串包含（短 cell 嵌在长 OCR 文本中）给完整分；
                    # 否则用文本相似度容忍形近字/空格等细微差异（ISWM 文本分量）。
                    # 注意不能用 text_similarity 完全替代子串：difflib 长度归一化
                    # 会让「短 cell 是长 OCR 文本的子串」得分极低（<0.5）而漏配。
                    if ot_norm in vt_norm or vt_norm in ot_norm:
                        score += min(len(ot_norm), len(vt_norm))
                    else:
                        sim = text_similarity(ot_norm, vt_norm)
                        if sim >= 0.5:
                            score += sim * min(len(ot_norm), len(vt_norm))
            if score > best_score:
                best_score = score
                best_row = vi
        row_anchor.append(best_row)
        if (
            best_row is not None
            and ocr_row_y is not None
            and oi < len(ocr_row_y)
        ):
            anchored_y.append(ocr_row_y[oi])

    # G6A y-extent：锚定行的 y 跨度（向外扩一行行距容差）。
    # 行距取 OCR 网格相邻行 y 间距的中位数（自适应截图缩放），容差=一行行距，
    # 保证紧邻锚定数据带的第一/末数据行不误伤；表外噪声（印章/页标题/相邻行）
    # 距数据带通常 ≥2 行距，位于容差之外被丢弃。至少 1 行锚定才启用，
    # 否则（OCR 与 VLM 无任何可锚定关系）退回既有跟随逻辑，避免误伤。
    g6a_min_y: float | None = None
    g6a_max_y: float | None = None
    if ocr_row_y is not None and len(anchored_y) >= 1:
        sorted_y = sorted(ocr_row_y)
        pitches = [
            high - low
            for low, high in zip(sorted_y, sorted_y[1:])
            if high > low
        ]
        row_pitch = (
            sorted(pitches)[len(pitches) // 2]
            if pitches
            else max(anchored_y) - min(anchored_y)
            if len(anchored_y) > 1
            else 30.0
        )
        y_tol = max(row_pitch, 12.0)
        g6a_min_y = min(anchored_y) - y_tol
        g6a_max_y = max(anchored_y) + y_tol

    current_vlm_row = data_row_start

    for oi, ocr_row in enumerate(ocr_grid):
        # 查找该 OCR 行最匹配的 VLM 数据行
        best_row = row_anchor[oi]
        anchored_now = False

        if best_row is not None:
            current_vlm_row = best_row
            anchored_now = True
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
                    anchored_now = True

        # G6A：从未锚定到任何 VLM 行（无自身锚点且 peek-ahead 无果，仅跟随
        # 上一个锚定行）、且 y 中心落在锚定行数据带之外的行，判为表格区域外
        # 噪声（印章/页标题/相邻行），整行丢弃不进入行池。
        if (
            not anchored_now
            and g6a_min_y is not None
            and oi < len(ocr_row_y)
            and (ocr_row_y[oi] < g6a_min_y or ocr_row_y[oi] > g6a_max_y)
        ):
            continue

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
    # [自定义] 剔除值间空白（如 "1.78640768223\n1.11650431565" 中的 \n）。
    # 本函数只接受 \d+\.\d+ 纯数值，空白无语义，剔除后按等精度切分不受影响。
    data = re.sub(r"\s+", "", data)
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


def _split_integer_quantity(
    digits: str,
    unit_prices: list[str],
    amounts: list[str],
) -> list[str]:
    """用「金额 = 数量 × 单价」反推整数数量列的拆分。

    VLM 将整数数量（无小数点，如 "3332"+"19197"→"333219197"）拼接为单个
    纯数字串，无法按小数位数切分。单价（等精度）与金额（2 位小数）两列可
    独立可靠拆分，故用每行 ``round(金额/单价)`` 反推数量，并双重校验：
    （1）反推值 × 单价 ≈ 金额（容差 0.01 元或金额的 0.1%）；
    （2）反推数量拼接后还原原始整数串。任一校验失败返回空列表，调用方
    回退 OCR 重建（原则 4：双重约束保证必然正确，绝不硬猜）。

    Args:
        digits: 整数数量拼接串（仅数字，如 "333219197"）。
        unit_prices: 已拆分的单价列表（长度 n_data）。
        amounts: 已拆分的金额列表（长度 n_data）。

    Returns:
        反推出的数量字符串列表；无法可靠反推返回空列表。
    """
    if len(digits) < 2 or len(unit_prices) < 2 or len(amounts) != len(unit_prices):
        return []
    quantities: list[str] = []
    for up, amt in zip(unit_prices, amounts):
        try:
            price = float(up)
            amount = float(amt)
            if price == 0:
                return []
            q = round(amount / price)
        except (ValueError, OverflowError):
            return []
        tol = max(0.01, 0.001 * abs(amount))
        if abs(q * price - amount) > tol:
            return []
        quantities.append(str(q))
    if "".join(quantities) != digits:
        return []
    return quantities


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


__all__ = [
    '_INVOICE_DATA_COLUMN_KEYWORDS',
    '_NUMERIC_COLUMN_KEYWORDS',
    '_align_ocr_to_vlm_rows',
    '_append_text_to_cell',
    '_build_ocr_text_grid',
    '_count_ocr_data_rows',
    '_detect_data_row_start',
    '_find_ocr_header_row',
    '_get_fillable_columns',
    '_has_concatenated_data_cells',
    '_is_numeric_column',
    '_map_ocr_cols_to_vlm_cols',
    '_match_data_column_keyword',
    '_merge_split_header_chars',
    '_parse_vlm_table_structure',
    '_split_decimal_values',
    '_split_integer_quantity',
    '_split_name_cell',
    '_try_split_concatenated_numbers',
]
