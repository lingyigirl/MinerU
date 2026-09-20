"""鬼影表与印章噪声守卫。

在 OCR 结果进入补充流程前做可信度判定，避免花屏截图、
印章文本被误当作表格数据写入单元格。"""

import re
from loguru import logger


# Hybrid 模式表格 OCR 补全（方案 B：Pipeline OCR 补充 VLM 表格）
# 守卫：鬼影表与印章噪声过滤


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
    if not text or re.search(r'\d', text):
        return False
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    return cjk >= 1 and len(text) <= 3 and cjk == len(text)


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


__all__ = [
    '_filter_seal_overlap_tokens',
    '_is_ghost_table',
    '_is_seal_like_text',
]
