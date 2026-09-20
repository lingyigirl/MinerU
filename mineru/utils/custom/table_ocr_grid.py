"""OCR 网格构建与表格单元格补充。

从 table_utils.py 拆分出的 OCR 相关逻辑：OCR 文本网格构建、
VLM 行对齐、空单元格填充、数值/文本守卫、印章/鬼影检测。
"""

import os
import re
import unicodedata
from bs4 import BeautifulSoup, NavigableString, Tag
from loguru import logger

# 跨文件导入（table_utils.py 是本模块的根依赖，不反向导入）
from mineru.utils.custom.table_utils import _is_data_value, _is_invoice_table, _INVOICE_HEADER_KEYWORDS

# ============================================================
# Hybrid 模式表格 OCR 补全（方案 B：Pipeline OCR 补充 VLM 表格）
# ============================================================


def _is_ghost_table(ocr_results: list, min_data_tokens: int = 10) -> bool:
    """判断 OCR 结果是否来自鬼影/花屏的表格截图（守卫 7-2）。

    部分 PDF 页面渲染后表格截图为鬼影图像，OCR 在其上读到的是固定
    位置重复的乱码碎片（数字/符号/单字），而非表格真实数据。
    检测特征：
    1. 数据区 token 中 CJK 占比极低（<30%），以乱码数字/符号为主
    2. 相同 x 位置的 token 在不同 y 行反复出现（渲染残影，重复率 >15%）

    Args:
        ocr_results: PaddleOCR 原始输出列表。
        min_data_tokens: 最低数据 token 数，低于此值不判定鬼影。

    Returns:
        是否为鬼影表格。
    """
    import re
    from collections import Counter

    # 提取 (y_min, x_center, text) 三元组
    items = []
    for res in ocr_results:
        try:
            bbox = res[0]
            text_info = res[1]
            text = str(text_info[0] if isinstance(text_info, (list, tuple)) else text_info).strip()
            if not text:
                continue
            if isinstance(bbox[0], (list, tuple)):
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
            else:
                xs = [bbox[i] for i in range(0, len(bbox), 2)]
                ys = [bbox[i] for i in range(1, len(bbox), 2)]
            items.append((min(ys), (min(xs) + max(xs)) / 2, text))
        except (IndexError, TypeError, ValueError):
            continue

    if len(items) < min_data_tokens:
        return False

    # 按 y 排序，定位表头/数据分隔点
    ys_sorted = sorted({y for y, _, _ in items})
    if len(ys_sorted) <= 2:
        return False

    # 取 y 排序后的第 3 个 cluster 作为数据区阈值（跳过表头行）
    if len(ys_sorted) >= 5:
        y_threshold = sorted(ys_sorted)[:3][-1]
    elif len(ys_sorted) >= 3:
        y_threshold = sorted(ys_sorted)[:2][-1]
    else:
        y_threshold = 0
    data_items = [(x, t) for y, x, t in items if y > y_threshold]

    if not data_items:
        return False

    # 特征 1：CJK 占比
    cjk_count = sum(1 for _, t in data_items if re.search(r'[一-鿿]', t))
    cjk_ratio = cjk_count / len(data_items)

    # 特征 2：x 位置重复率（x 四舍五入到 20px 区间）
    x_buckets = [round(x / 20) * 20 for x, _ in data_items]
    bucket_freq = Counter(x_buckets)
    max_dup = max(bucket_freq.values()) if bucket_freq else 0
    x_dup_ratio = max_dup / len(data_items)

    # 特征 3：单字 CJK 占比（鬼影中单 CJK 残影多，真实数据多 CJK 词）
    cjk_single = sum(1 for _, t in data_items if len(re.findall(r'[一-鿿]', t)) == 1)
    single_cjk_ratio = cjk_single / max(cjk_count, 1) if cjk_count > 0 else 0

    # 花屏判定：
    #   (a) 数据区低 CJK (<30%) + 同 x 高重复 → classic ghost
    #   (b) 数据区单字 CJK 为主 >50% + 同 x 高重复 → CJK artifact ghost
    is_ghost = (cjk_ratio < 0.3 and x_dup_ratio > 0.15) or \
               (cjk_ratio < 0.5 and x_dup_ratio > 0.15 and single_cjk_ratio > 0.5)
    if is_ghost:
        logger.debug(
            f"鬼影检测: CJK={cjk_count}/{len(data_items)}={cjk_ratio:.2%}, "
            f"单CJK={cjk_single}/{cjk_count}={single_cjk_ratio:.2%}, "
            f"x-dup={max_dup}/{len(data_items)}={x_dup_ratio:.2%}"
        )
    return is_ghost


def _is_seal_like_text(text: str) -> bool:
    """判断文本是否为印章短 CJK 碎片（如「子回」「单」「用」）。

    印章碎片特征：纯 CJK、≤3 字符、无数字。合法的表格数值
    （如「3,267.39」「泰安市泰安区」）即使落在印章重叠区内也保留。

    Args:
        text: 待判断的文本（strip 后）。

    Returns:
        是否为印章短 CJK 碎片。
    """
    import re
    if not text or re.search(r'\d', text):
        return False
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    return cjk >= 1 and len(text) <= 3 and cjk == len(text)



def _digits_only(text: str) -> str:
    """提取字符串中的数字字符，用于数值 token 的同行归一化比对（守卫 8）。

    去除非数字字符后比对，可消去不同语言区域的标点/分隔符差异
    （如 '3.649.36' vs '3,649.36' → digits '364936' == '364936'）。

    Args:
        text: 待提取的文本。

    Returns:
        仅含数字字符的字符串（无数字时返回空串）。
    """
    return re.sub(r"\D", "", text)


def _is_same_row_value_variant(ot: str, vlm_row: list[str]) -> bool:
    """判断数值 token 是否为同行已有值的 OCR 变体（守卫 8）。

    同行已有值的变体不是新信息，不得写入空列（原则 1 输出不多不少）。
    覆盖三种匹配模式：
      ① 数字归一化相等：3.649.36 ≡ 3,649.36（标点/分隔符差异）
      ② 截断残片：,300.75 ⊂ 8,300.75、10,000,000 ⊂ 10,000,000.00（长度差 ≤2）
      ③ 漏位/形近：1511101040027417 ← 15511101040027417（编辑距离 ≤1, min 长度 ≥6 位）

    Args:
        ot: OCR 识别 token 文本。
        vlm_row: VLM 同行所有非空单元格文本列表。

    Returns:
        是否为同行已有值的 OCR 变体。
    """
    d_ot = _digits_only(ot)
    if not d_ot:
        return any(ot == vt for vt in vlm_row if vt)
    for vt in vlm_row:
        if not vt:
            continue
        d_vt = _digits_only(vt)
        if not d_vt:
            continue
        # ① 数字归一化相等
        if d_ot == d_vt:
            return True
        # ② 截断残片（短的一侧 ≥5 位，长度差 ≤2）
        shorter, longer = (d_ot, d_vt) if len(d_ot) <= len(d_vt) else (d_vt, d_ot)
        if (
            len(shorter) >= 5
            and len(longer) - len(shorter) <= 2
            and (longer.startswith(shorter) or longer.endswith(shorter))
        ):
            return True
        # ③ 漏位/形近（两侧均 ≥6 位，编辑距离 ≤1）
        if len(d_ot) >= 6 and len(d_vt) >= 6 and _edit_distance_le1(d_ot, d_vt):
            return True
    return False


def _filter_seal_overlap_tokens(
    ocr_results: list,
    seal_overlaps_page: list,
    table_bbox: list,
    img_w: int,
    img_h: int,
) -> list:
    """过滤掉落在印章重叠区域内的 OCR token（守卫 7-1）。

    印章块与表格块在页面坐标系中存在 bbox 重叠时，表格截图包含印章图像，
    OCR 会把印章文字读入 token 池。本函数将重叠区从页面坐标变换到截图
    坐标，然后移出中心点落入该区域的所有 token。

    Args:
        ocr_results: PaddleOCR (det+rec) 原始输出。
        seal_overlaps_page: 印章-表格重叠区（页面坐标系），[(x1,y1,x2,y2), ...]。
        table_bbox: 表格 bbox（页面坐标系），[x1,y1,x2,y2]。
        img_w: 表格截图宽度（像素）。
        img_h: 表格截图高度（像素）。

    Returns:
        过滤后的 OCR 结果；每个参数不满足条件时返回原列表。
    """
    if not seal_overlaps_page or not table_bbox or len(table_bbox) < 4 or not img_w:
        return ocr_results

    # 页面→图片坐标缩放因子
    tw = max(table_bbox[2] - table_bbox[0], 1)
    scale_x = img_w / tw
    if not img_h or img_h < 10:
        # 图片高度未知时，以宽度缩放比例作为 y 轴缩放
        scale_y = scale_x
    else:
        scale_y = img_h / max(table_bbox[3] - table_bbox[1], 1)

    # 将重叠区转换到图片坐标系
    overlaps_img = []
    for ox1, oy1, ox2, oy2 in seal_overlaps_page:
        ix1 = (ox1 - table_bbox[0]) * scale_x
        iy1 = (oy1 - table_bbox[1]) * scale_y
        ix2 = (ox2 - table_bbox[0]) * scale_x
        iy2 = (oy2 - table_bbox[1]) * scale_y
        overlaps_img.append((ix1, iy1, ix2, iy2))

    filtered = []
    for res in ocr_results:
        try:
            bbox = res[0]
            if isinstance(bbox[0], (list, tuple)):
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
            else:
                xs = [bbox[i] for i in range(0, len(bbox), 2)]
                ys = [bbox[i] for i in range(1, len(bbox), 2)]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            in_overlap = any(
                ix1 <= cx <= ix2 and iy1 <= cy <= iy2
                for ix1, iy1, ix2, iy2 in overlaps_img
            )
            if not in_overlap:
                filtered.append(res)
            else:
                txt = str(res[1][0] if isinstance(res[1], (list, tuple)) else res[1])
                stripped = txt.strip()
                # 仅过滤印章短 CJK 碎片；合法表格值（含数字、长文本）保留
                if stripped and _is_seal_like_text(stripped):
                    logger.debug(
                        f"守卫 7-1 印章重叠区过滤: '{txt}' "
                        f"@({cx:.0f},{cy:.0f}) 重叠区=({ix1:.0f},{iy1:.0f})-({ix2:.0f},{iy2:.0f})"
                    )
                else:
                    # 数字/空格/合法长文本 → 保留
                    filtered.append(res)
        except (IndexError, TypeError, ValueError):
            filtered.append(res)

    logger.debug(f"守卫 7-1: {len(ocr_results)}→{len(filtered)} 个 OCR token")
    return filtered


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
    _ocr_min_conf = float(os.getenv("MINERU_TABLE_OCR_MIN_CONFIDENCE", "0.8"))
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
            item_type = _classify_ocr_item_type(ot)
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
            else:
                # [自定义] 守卫 8：数值/税率与同行的归一化变体比对去重。
                # 数值可合法重复出现（跨行），但**同行**内的 OCR 变体不是新信息。
                if _is_same_row_value_variant(ot, vlm_row):
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
            # 守卫 2：不再用 mode=="empty" 通配——空单元格只接受与列类型一致
            # 的 OCR token。number/rate 类 与 表头推断出的 text 类空列不匹配，
            # 防止 OCR 数值噪声（如 "0.0"、"106,270.070005800001"）灌入
            # 「对方户名/摘要」等文本空列（输出不多不少）。
            for fi, (vc, ct, mode) in enumerate(fillable):
                cell_tag = vlm_tag_row[vc]
                if mode == "empty" and id(cell_tag) in filled_cell_ids:
                    continue
                if ocr_type == ct:
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
            # 守卫 2-兜底：number 类 OCR token 不得落入表头推断为 text 的列；
            # text 类 OCR token（印章碎片/行标签截断）不得落入 number/rate 列，
            # 防止列类型修复后（如"转出金额"→number）的 name→number 跨类型污染。
            for fi, (vc, ct, mode) in enumerate(fillable):
                if mode == "empty":
                    if ct == "text" and ocr_type == "number":
                        continue
                    if ct in ("number", "rate") and ocr_type == "text":
                        continue
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

def _is_pure_punctuation(text: str) -> bool:
    """判断文本是否为无信息量的短标点噪声（如「。」「、」「-」「..」）。

    用于在 OCR 文本过滤阶段排除纯标点噪声。判断两重：
    1. 全为标点（P）/符号（S）/空白（Z）字符；
    2. 长度 ≤ 2——「***」这类多字符纯符号串是银行流水/发票中的
       账号打码掩码（真实内容），不能丢弃（输出不少），仅丢弃短噪声。

    Args:
        text: 待检查的原始 OCR 文本。

    Returns:
        是否是无信息量的短标点噪声。
    """
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) > 2:
        return False
    return all(
        unicodedata.category(ch).startswith(("P", "Z", "S"))
        for ch in stripped
    )


def _is_single_cjk_char(text: str) -> bool:
    """判断规范化后的文本是否为单一个中文字符。

    Args:
        text: 规范化后的文本。

    Returns:
        是否恰为单个 CJK 字符。
    """
    return len(text) == 1 and "一" <= text <= "鿿"


def _is_merged_noise(text: str) -> bool:
    """检测 OCR 横向合并相邻单元格产生的拼接噪音。

    OCR 把相邻格读成单框且丢失分隔符，产生无合法语义的拼接串：
    模式 M1: MM-DD 紧接非数字内容（"01-06收"、"01-02对公收费"）
             —— 日期 `MM-DD` 后应只有空白/行尾，紧接其它字符说明
             跨了相邻列；`D`（非数字）守卫保证 `12-3456789` 这类
             账号不误伤。
    模式 M2: YYYY-MM-DD 紧接 HH:MM 无空格（"2025-02-1416:53:28"）
             —— 源 PDF 中交易日期与时间之间必有空格，无空格拼接
             即 OCR 丢失分隔符（合法时间串 "2025-02-14 16:53:28"
             中间是空格，不匹配）。

    Args:
        text: 待检查的原始 OCR 文本。

    Returns:
        是否为拼接噪音。
    """
    return bool(
        re.match(r"^\d{2}-\d{2}\D", text)
        or re.match(r"^\d{4}-\d{2}-\d{2}\d{2}:\d{2}", text),
    )


# 守卫 5 用：语义源（页面 chrome）文本正则
_CJK_RE = re.compile(r"[一-鿿]")
_SEAL_NOISE_CHARS = re.compile(r"[\s，。、,.:：（）()%％—-]")


def _compact_norm(text: str) -> str:
    """去空白与分隔标点后的紧凑文本（印章/标题/截断比对用）。

    Args:
        text: 原始文本。

    Returns:
        去除空白与 `，。、,.:：（）()%％—-` 后的字符串。
    """
    return _SEAL_NOISE_CHARS.sub("", text or "")


def _cjk_count(text: str) -> int:
    """统计文本中的 CJK 统一表意文字个数。"""
    return len(_CJK_RE.findall(text or ""))


def _edit_distance_le1(a: str, b: str) -> bool:
    """判断两个规范化字符串的编辑距离是否 ≤1（早期退出）。

    用于 OCR 印章误识近匹配（如「枣庄三八支行」→「本庄三八支行」：
    枣→本 单字符替换）。长度差 >1 即不可能是 1 次编辑，直接返回。

    Args:
        a: 规范化后的字符串。
        b: 规范化后的字符串。

    Returns:
        编辑距离 ≤1 时为 True。
    """
    if abs(len(a) - len(b)) > 1:
        return False
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(m):
        prev = dp
        dp = [i + 1] + [0] * n
        for j in range(n):
            if a[i] == b[j]:
                cost = 0
            else:
                cost = 1
            dp[j + 1] = min(
                prev[j + 1] + 1,  # 删除 a[i]
                dp[j] + 1,  # 插入 b[j]
                prev[j] + cost,  # 替换
            )
        if min(dp) > 1:
            return False
    return dp[n] <= 1


