"""本地 KVP 提取引擎（基于 PaddleOCR + 空间距离配对 + 可插拔标签词典）。

不依赖 PP-StructureV3 或任何外部 API，使用项目自带的 PytorchPaddleOCR
提取文本 + bbox，通过空间距离和标签词典配对 label-value。

支持的文档布局模式：
    A. "标签：值" 冒号分隔 → 直接解析
    B. "标签+值" 无分隔符拼接 → 正则拆分（如 "客户号10198594700"）
    C. 标签在值上方（表头行 → 数据行网格布局）
    D. 值在标签上方（表单布局，值填入格子内，标签在格子下方）
    E. 同行左右配对（标签左，值右）
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

import numpy as np
from loguru import logger


# ---------------------------------------------------------------------------
# OCR 文本提取
# ---------------------------------------------------------------------------

def _ocr_extract_boxes(
    pil_img,
    lang: str = "ch",
) -> list[dict[str, Any]]:
    """运行 PaddleOCR 提取全部 text box。

    复用 S1 分类器的 OCR 单例（get_ocr），避免 KVP 管线中重复初始化模型。
    """
    from mineru.utils.custom.doc_classifier import get_ocr

    ocr = get_ocr()
    results = ocr.ocr(np.asarray(pil_img))

    if not results or not results[0]:
        return []

    boxes = []
    for item in results[0]:
        if not item or len(item) < 2:
            continue
        pts = item[0]
        text_info = item[1]
        if not text_info or not text_info[0]:
            continue

        text = text_info[0].strip()
        confidence = float(text_info[1]) if len(text_info) > 1 else 0.0
        if not text or confidence < 0.5:
            continue

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)

        boxes.append({
            "text": text,
            "bbox": (x1, y1, x2, y2),
            "confidence": confidence,
            "cx": (x1 + x2) / 2,
            "cy": (y1 + y2) / 2,
            "x1": x1, "x2": x2, "y1": y1, "y2": y2,
        })

    return boxes


# ---------------------------------------------------------------------------
# 标签匹配
# ---------------------------------------------------------------------------

def _match_label(
    text: str,
    compiled_labels: list[tuple[re.Pattern, str]],
) -> Optional[str]:
    """尝试将文本匹配到已知标签词典。"""
    text_clean = text.strip().rstrip("：:：").strip()
    for pattern, label in compiled_labels:
        if pattern.search(text_clean):
            return label
    return None


def _try_split_colon(text: str) -> tuple[Optional[str], Optional[str]]:
    """尝试从 "标签：值" 格式解析。"""
    m = re.match(r"^(.+?)[：:]\s*(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _try_split_concatenated(
    text: str,
    compiled_labels: list[tuple[re.Pattern, str]],
) -> tuple[Optional[str], Optional[str]]:
    """尝试拆分 "标签值" 无分隔符拼接的文本。

    如 "客户号10198594700" → ("客户号", "10198594700")
    如 "币种人民币" → ("币种", "人民币")
    如 "自动转存不转存" → ("自动转存", "不转存")

    修复：收集所有匹配 → 按匹配长度降序 → 取最长匹配。
    若最长匹配的剩余为空或以括号开头，说明是纯标签（如"存入金额（大写）"），
    不拆分，返回 (None, None)。

    Args:
        text: OCR 文本框文本。
        compiled_labels: 编译后的标签词典。

    Returns:
        (label, value)。无法拆分时返回 (None, None)。
    """
    text = text.strip()
    if not text or len(text) < 4:
        return None, None

    # 收集所有匹配，记录匹配结束位置
    matches: list[tuple[int, str]] = []
    for pattern, label in compiled_labels:
        m = pattern.match(text)  # 用 match（前缀匹配）而非 search
        if m:
            matches.append((m.end(), label))

    if not matches:
        return None, None

    # 按匹配长度降序排列（最长匹配优先）
    matches.sort(key=lambda x: -x[0])

    for match_end, label in matches:
        value = text[match_end:].strip()

        # 完整匹配（value 为空）→ 纯标签，不拆分
        if not value:
            return None, None

        # 剩余以括号开头 → 是标签修饰如"（大写）/ (小写)"，非真实值
        # 尝试下一个（更短的）匹配
        if value[0] in '（(':
            continue

        # 有效拆分：标签 + 值
        return label, value

    return None, None


# ---------------------------------------------------------------------------
# 空间距离计算
# ---------------------------------------------------------------------------

def _box_distance(
    box_a: dict,
    box_b: dict,
    x_weight: float = 0.3,
) -> float:
    """计算两个 box 之间的加权空间距离。

    X 方向权重较低（允许较宽的水平配对），Y 方向更敏感。

    Args:
        box_a, box_b: 两个 box。
        x_weight: X 方向权重（0~1）。

    Returns:
        加权欧氏距离。
    """
    dx = abs(box_a["cx"] - box_b["cx"])
    dy = abs(box_a["cy"] - box_b["cy"])
    return math.sqrt((dx * x_weight) ** 2 + dy ** 2)


def _box_distance_v2(
    box_a: dict,
    box_b: dict,
    x_weight: float = 0.3,
    mode: str = "label_above",
) -> float:
    """优化版加权空间距离（v2）。

    相对 :func:`_box_distance` 的两点改进：
    1. X 重叠比衰减——两框 X 重叠越大，距离越近（最多衰减 50%），
       利用位置信息而非仅中心距。
    2. 策略感知的垂直偏移惩罚——按配对模式对不合预期的垂直落差加罚分。

    Args:
        box_a, box_b: 两个 box。
        x_weight: 基础 X 方向权重。
        mode: 配对模式（"label_above"=策略C / "value_above"=策略D / "same_row"=策略E）。

    Returns:
        加权距离（含重叠衰减与策略惩罚）。
    """
    dx = abs(box_a["cx"] - box_b["cx"])
    dy = abs(box_a["cy"] - box_b["cy"])
    base_dist = math.sqrt((dx * x_weight) ** 2 + dy ** 2)

    # 1. X 重叠比衰减（重叠越多距离越小，最多衰减 50%）
    x_overlap = _x_overlap_ratio(box_a, box_b)
    base_dist *= 1.0 - x_overlap * 0.5

    # 2. 策略感知的垂直偏移惩罚
    if mode in ("label_above", "value_above"):
        # 期望值在标签附近（20~80px 落差），过近/过远都惩罚
        if dy < 5 or dy > 80:
            base_dist += 100.0
    elif mode == "same_row":
        # 期望同水平行，垂直偏差超 10px 重罚
        if dy > 10:
            base_dist += 200.0

    return base_dist


def _x_overlap_ratio(box_a: dict, box_b: dict) -> float:
    """计算两个 box 的 X 范围重叠比例。"""
    overlap = min(box_a["x2"], box_b["x2"]) - max(box_a["x1"], box_b["x1"])
    if overlap <= 0:
        return 0.0
    span = max(box_a["x2"] - box_a["x1"], box_b["x2"] - box_b["x1"])
    return overlap / span if span > 0 else 0.0


def _merge_multiline_values(
    values: list[dict],
    x_overlap_threshold: float = 0.7,
    max_dy: float = 50.0,
) -> list[dict]:
    """合并 OCR 拆分的多行值框。

    长字段（地址、单位名称等）常被 OCR 拆成多行独立文本框，本函数把
    「垂直相邻 + X 高度重叠」的值框合并为单个值框（文本空格拼接 + bbox 取并集），
    避免后续空间配对把同一字段的各行错配给不同标签。

    Args:
        values: 值框列表（含 text/cx/cy/x1/y1/x2/y2）。
        x_overlap_threshold: X 重叠比阈值。
        max_dy: 最大垂直间距（像素）。

    Returns:
        合并后的值框列表（未合并的原样保留）。
    """
    if len(values) < 2:
        return values

    sorted_values = sorted(values, key=lambda b: (b["cy"], b["cx"]))
    merged: list[dict] = []
    i = 0
    while i < len(sorted_values):
        cur = dict(sorted_values[i])
        j = i + 1
        while j < len(sorted_values):
            nxt = sorted_values[j]
            dy = abs(nxt["cy"] - cur["cy"])
            if dy >= max_dy:
                break
            if _x_overlap_ratio(cur, nxt) < x_overlap_threshold:
                break  # 不重叠：后续更远，终止当前合并链，nxt 作为下一个簇起点
            # 合并文本 + bbox（取并集）
            cur["text"] = cur["text"] + " " + nxt["text"]
            cur["x1"] = min(cur["x1"], nxt["x1"])
            cur["y1"] = min(cur["y1"], nxt["y1"])
            cur["x2"] = max(cur["x2"], nxt["x2"])
            cur["y2"] = max(cur["y2"], nxt["y2"])
            cur["cx"] = (cur["x1"] + cur["x2"]) / 2
            cur["cy"] = (cur["y1"] + cur["y2"]) / 2
            cur["bbox"] = (cur["x1"], cur["y1"], cur["x2"], cur["y2"])
            j += 1
        merged.append(cur)
        i = j
    return merged


# ---------------------------------------------------------------------------
# 网格布局值拆分（D 阶段）
# ---------------------------------------------------------------------------

def _pre_split_grid_values(
    labels: list[dict],
    values: list[dict],
    kvp: dict[str, Any],
    kvp_bboxes: dict[str, dict[str, Any]],
) -> None:
    """C0 阶段：在空间配对之前，检测并拆分 OCR 合并的多列网格值。

    当一行中多个标签的值被 OCR 合并为一个宽文本框时（如网格布局），
    利用标签 x 位置作为列边界，按比例将合并文本拆分为各字段独立值。
    拆分后从 values 列表中移除已消耗的宽值框，防止 C 阶段错误配对。

    Args:
        labels: 所有标签 box 列表（含 matched_label）。
        values: 所有值 box 列表（会被原地修改，移除已拆分的宽值框）。
        kvp: KVP 结果 dict（会被修改）。
        kvp_bboxes: KVP bbox 记录 dict（会被修改）。
    """
    if len(labels) < 2 or len(values) < 2:
        return

    # C0-1: 标签按 y 聚类成行（容差 30px）
    labels_sorted = sorted(labels, key=lambda b: (b["cy"], b["cx"]))
    rows: list[list[dict]] = []
    for lbl in labels_sorted:
        placed = False
        for row in rows:
            row_cy = sum(b["cy"] for b in row) / len(row)
            if abs(lbl["cy"] - row_cy) < 30:
                row.append(lbl)
                placed = True
                break
        if not placed:
            rows.append([lbl])

    # 每行内按 x 排序
    for row in rows:
        row.sort(key=lambda b: b["cx"])

    MIN_MERGE_WIDTH = 150   # 最小合并宽度（像素）
    indices_to_remove: set[int] = set()

    for row in rows:
        if len(row) < 2:
            continue

        row_cy = sum(lbl["cy"] for lbl in row) / len(row)

        for vi, val_box in enumerate(values):
            if vi in indices_to_remove:
                continue

            val_w = val_box["x2"] - val_box["x1"]
            if val_w < MIN_MERGE_WIDTH:
                continue

            # 值的 y 应在标签行下方 10~120px 区间
            if not (row_cy + 10 < val_box["cy"] < row_cy + 120):
                continue

            # 值框跨越 ≥2 个标签列
            covering_labels = [
                lbl for lbl in row
                if val_box["x1"] - 30 < lbl["cx"] < val_box["x2"] + 30
                and lbl["matched_label"] not in kvp  # 已有值的标签不参与拆分
            ]
            if len(covering_labels) < 2:
                continue

            # C0-4: 按比例拆分字符串
            merged_text = val_box["text"]
            total_chars = len(merged_text)

            # 启发式：2 列等宽拆分（如双日期字段"2024053120240531"→8+8）
            if len(covering_labels) == 2:
                mid = total_chars // 2
                mid_x = val_box["x1"] + (val_box["x2"] - val_box["x1"]) * (mid / total_chars) if total_chars else val_box["cx"]
                for i, lbl in enumerate(covering_labels):
                    if i == 0:
                        portion = merged_text[:mid].strip()
                        est_bbox = [val_box["x1"], val_box["y1"], mid_x, val_box["y2"]]
                    else:
                        portion = merged_text[mid:].strip()
                        est_bbox = [mid_x, val_box["y1"], val_box["x2"], val_box["y2"]]
                    if portion:
                        label_name = lbl["matched_label"]
                        if label_name not in kvp:
                            kvp[label_name] = portion
                            kvp_bboxes[label_name] = {"merged_bbox": est_bbox}
                            logger.debug(
                                f"网格拆分(2列): '{label_name}' ← '{portion}' "
                                f"(来自 '{merged_text[:30]}...')"
                            )
                indices_to_remove.add(vi)
                continue

            # 构建列的 x 边界列表
            col_cx = [lbl["cx"] for lbl in covering_labels]
            # 列边界 = 相邻标签中心的中点
            boundaries: list[float] = [val_box["x1"]]
            for i in range(len(col_cx) - 1):
                boundaries.append((col_cx[i] + col_cx[i + 1]) / 2)
            boundaries.append(val_box["x2"])

            # 总宽度（在 val_box 内的实际跨度）
            total_w = boundaries[-1] - boundaries[0]
            if total_w <= 0:
                continue

            # 计算各列宽度比例
            ratios = []
            for i in range(len(covering_labels)):
                col_w = boundaries[i + 1] - boundaries[i]
                ratios.append(max(col_w / total_w, 0.04))

            # 归一化
            ratio_sum = sum(ratios)
            ratios = [r / ratio_sum for r in ratios]

            # 按比例分配字符
            start = 0
            for i, lbl in enumerate(covering_labels):
                char_count = max(1, round(total_chars * ratios[i]))
                end = min(start + char_count, total_chars)
                if i == len(covering_labels) - 1:
                    end = total_chars

                portion = merged_text[start:end].strip()
                # 估算该分块的 bbox（按字符比例 + 列边界）
                est_x1 = boundaries[i] if i < len(boundaries) - 1 else val_box["x1"]
                est_x2 = boundaries[i + 1] if i + 1 < len(boundaries) else val_box["x2"]
                est_bbox = [est_x1, val_box["y1"], est_x2, val_box["y2"]]

                if portion:
                    label_name = lbl["matched_label"]
                    if label_name not in kvp:
                        kvp[label_name] = portion
                        kvp_bboxes[label_name] = {"merged_bbox": est_bbox}
                        logger.debug(
                            f"网格拆分: '{label_name}' ← '{portion}' "
                            f"(来自合并文本 '{merged_text[:40]}...')"
                        )
                start = end

            indices_to_remove.add(vi)

    # 从 values 列表中移除已拆分的宽值框（倒序删除）
    if indices_to_remove:
        for vi in sorted(indices_to_remove, reverse=True):
            del values[vi]
        logger.debug(f"网格预拆分: 移除了 {len(indices_to_remove)} 个宽值框")

def _is_label_modifier(text: str, compiled_labels: list[tuple[re.Pattern, str]]) -> bool:
    """检查文本是否为标签修饰符（如"大写"/"小写"）而非真实的字段值。

    当 OCR 将"大写""小写"等文字识别为独立文本框时，它们不应作为任何字段
    的值。这些文本是金额的修饰说明（"金额大写"），真正的值是"柒佰元整"等。

    Args:
        text: 待检查的文本。
        compiled_labels: 编译后的标签词典。

    Returns:
        True 如果文本是标签修饰符。
    """
    text_clean = text.strip()
    # 纯修饰符：这些文本本身不包含任何有效数据
    modifier_texts = {"大写", "小写"}
    if text_clean in modifier_texts:
        return True
    # 如果文本本身能被匹配为已知标签（如"金额(大写)"、"金额(小写)"）
    # 则它更可能是标签而非值
    for pattern, _ in compiled_labels:
        if pattern.fullmatch(text_clean):
            return True
    return False


def _box_to_list(box: dict) -> list[float]:
    """将 box 的 bbox 坐标转换为 [x1, y1, x2, y2] 列表。"""
    return [float(box["x1"]), float(box["y1"]), float(box["x2"]), float(box["y2"])]


def _merge_bboxes(*bboxes: list[float]) -> list[float]:
    """合并多个 bbox 为最小包围盒。"""
    if not bboxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]


def _normalize_bbox(
    bbox: list[float],
    page_w: float,
    page_h: float,
) -> list[int]:
    """将像素坐标 bbox 归一化到 1000 单位坐标系（对齐 hybrid_auto 的 content_list 格式）。

    hybrid_auto 在 make_blocks_to_content_list 阶段将像素坐标归一化到
    1000 单位（int(x * 1000 / page_width)），本函数遵循相同约定。

    Args:
        bbox: [x0, y0, x1, y1] 像素坐标（浮点数）。
        page_w: 页面像素宽度。
        page_h: 页面像素高度。

    Returns:
        [x0, y0, x1, y1] 归一化到 1000 单位的整数坐标。
    """
    x0, y0, x1, y1 = bbox
    return [
        int(x0 * 1000 / page_w),
        int(y0 * 1000 / page_h),
        int(x1 * 1000 / page_w),
        int(y1 * 1000 / page_h),
    ]


def _pair_form_layout(
    labels: list[dict],
    values: list[dict],
    compiled_labels: list[tuple[re.Pattern, str]],
    kvp: dict[str, Any],
    kvp_bboxes: dict[str, dict[str, Any]],
    paired_value_indices: set[int],
) -> int:
    """策略 D：表单布局配对（值在上，标签在下）。

    识别「值填入格子、标签在格子下方」的布局（银行申请表等）。
    对每个未配对标签，在其上方找 X 对齐、垂直最近的值框。

    配对规则（对应设计文档 §4.3）：
    1. 仅考虑 y 中心在标签上方的值框 (val.cy < lbl.cy)
    2. X 方向需有足够重叠: _x_overlap_ratio > 0.3
    3. 垂直距离 > 80px 的值框排除
    4. 取垂直距离最近者

    Args:
        labels: 标签框列表（含 matched_label）。
        values: 值框列表。
        compiled_labels: 编译后的标签正则列表。
        kvp: 结果字典（原地修改）。
        kvp_bboxes: bbox 记录字典（原地修改）。
        paired_value_indices: 已被占用的值框索引集合（原地修改）。

    Returns:
        成功配对的字段数。
    """
    paired = 0
    for lbl in labels:
        label_name = lbl["matched_label"]
        if label_name in kvp:
            continue

        best_vi = -1
        best_dist = float("inf")
        for vi, val in enumerate(values):
            if vi in paired_value_indices:
                continue
            # 值必须在标签上方
            if val["cy"] >= lbl["cy"]:
                continue
            # X 方向需有足够重叠
            if _x_overlap_ratio(lbl, val) <= 0.3:
                continue
            # 垂直距离超过 80px 排除
            if lbl["cy"] - val["cy"] > 80:
                continue
            dist = _box_distance_v2(lbl, val, mode="value_above")
            if dist < best_dist:
                best_dist = dist
                best_vi = vi

        if best_vi < 0:
            continue

        val_text = values[best_vi]["text"]
        if _is_label_modifier(val_text, compiled_labels):
            continue
        kvp[label_name] = val_text
        kvp_bboxes[label_name] = {
            "merged_bbox": _merge_bboxes(
                _box_to_list(lbl), _box_to_list(values[best_vi])
            )
        }
        paired_value_indices.add(best_vi)
        paired += 1
        logger.debug(f"表单布局配对(D): '{label_name}' ← '{val_text[:30]}'")
    return paired


def _pair_same_row(
    labels: list[dict],
    values: list[dict],
    compiled_labels: list[tuple[re.Pattern, str]],
    kvp: dict[str, Any],
    kvp_bboxes: dict[str, dict[str, Any]],
    paired_value_indices: set[int],
) -> int:
    """策略 E：同行左右配对（标签左，值右）。

    识别同一水平行内「标签在左、值在右」的模式（如合同/表格行）。

    配对规则（对应设计文档 §4.4）：
    1. 仅考虑同水平行的值框 (|dy| < 10px)
    2. 值在标签右侧 (lbl.cx < val.cx)
    3. 水平距离 > 300px 排除
    4. 取水平距离最近者

    Args:
        labels: 标签框列表（含 matched_label）。
        values: 值框列表。
        compiled_labels: 编译后的标签正则列表。
        kvp: 结果字典（原地修改）。
        kvp_bboxes: bbox 记录字典（原地修改）。
        paired_value_indices: 已被占用的值框索引集合（原地修改）。

    Returns:
        成功配对的字段数。
    """
    paired = 0
    for lbl in labels:
        label_name = lbl["matched_label"]
        if label_name in kvp:
            continue

        best_vi = -1
        best_dx = float("inf")
        for vi, val in enumerate(values):
            if vi in paired_value_indices:
                continue
            # 同水平行（容差 10px）
            if abs(lbl["cy"] - val["cy"]) >= 10:
                continue
            # 值在标签右侧
            if val["cx"] <= lbl["cx"]:
                continue
            dx = val["cx"] - lbl["cx"]
            if dx > 300:
                continue
            if dx < best_dx:
                best_dx = dx
                best_vi = vi

        if best_vi < 0:
            continue

        val_text = values[best_vi]["text"]
        if _is_label_modifier(val_text, compiled_labels):
            continue
        kvp[label_name] = val_text
        kvp_bboxes[label_name] = {
            "merged_bbox": _merge_bboxes(
                _box_to_list(lbl), _box_to_list(values[best_vi])
            )
        }
        paired_value_indices.add(best_vi)
        paired += 1
        logger.debug(f"同行左右配对(E): '{label_name}' ← '{val_text[:30]}'")
    return paired


def _pair_kvp(
    boxes: list[dict],
    compiled_labels: list[tuple[re.Pattern, str]],
) -> dict[str, Any]:
    """从 OCR text box 列表中配对 label → value。

    配对策略（按优先级）：
    A. "标签：值" 冒号分隔 → 直接解析
    B. "标签值" 无分隔符拼接 → 正则拆分
    多行值聚合 → 合并 OCR 拆分的多行值框
    C0. 网格预拆分 → 宽值框按标签列边界比例拆分
    C. 空间最近邻配对 → 标签找最近的未匹配值
    D. 表单布局配对 → 值在上、标签在下
    E. 同行左右配对 → 标签左、值右

    同时记录每个字段的 bbox 信息（label_bbox + value_bbox），
    供下游 middle_json 生成独立 span。

    Args:
        boxes: OCR text box 列表。
        compiled_labels: 编译后的标签词典。

    Returns:
        包含 KVP 字段值 + _kvp_bboxes 的 dict。
    """
    kvp: dict[str, Any] = {}
    # 记录每个字段的 bbox 信息
    kvp_bboxes: dict[str, dict[str, Any]] = {}

    # 分类每个 box
    labels: list[dict] = []    # 未匹配的标签 box
    values: list[dict] = []    # 未匹配的值 box

    for box in boxes:
        text = box["text"]
        box_bbox = _box_to_list(box)

        # A: 冒号分隔
        label_text, value_text = _try_split_colon(text)
        if label_text and value_text:
            matched_label = _match_label(label_text, compiled_labels)
            if matched_label:
                if matched_label not in kvp or box["confidence"] > 0.8:
                    kvp[matched_label] = value_text
                    kvp_bboxes[matched_label] = {"merged_bbox": box_bbox}
                continue
            # 未匹配到已知标签但格式正确 → 直接收录
            if label_text not in kvp:
                kvp[label_text] = value_text
                kvp_bboxes[label_text] = {"merged_bbox": box_bbox}
            continue

        # B: 无分隔符拼接
        concat_label, concat_value = _try_split_concatenated(text, compiled_labels)
        if concat_label and concat_value:
            if concat_label not in kvp or box["confidence"] > 0.8:
                kvp[concat_label] = concat_value
                kvp_bboxes[concat_label] = {"merged_bbox": box_bbox}
            continue

        # C: 待空间配对
        matched_label = _match_label(text, compiled_labels)
        if matched_label and matched_label not in kvp:
            labels.append({**box, "matched_label": matched_label})
        elif text and not matched_label:
            # 排除已知的噪声文本
            noise_patterns = [
                r"^\d+亿人都在用",
                r"^扫描全能王",
                r"^S扫描",
                r"^信[用g]",
                r"^[证凭]$",
                r"^[存定]$",
            ]
            is_noise = any(re.match(p, text) for p in noise_patterns)
            if not is_noise:
                values.append(box)

    # ---- 多行值聚合（在空间配对之前，合并 OCR 拆分的多行值框） ----
    values = _merge_multiline_values(values)

    # ---- C0: 网格预拆分（在空间配对之前，防止合并值被错误抢走） ----
    # 检测宽值框跨越多个标签列，按列比例拆分为各字段的独立值
    _pre_split_grid_values(labels, values, kvp, kvp_bboxes)

    # ---- C: 空间最近邻配对（仅对 C0 之后剩余的未匹配标签） ----
    MAX_PAIR_DISTANCE = 250.0   # 最大配对距离（像素）

    paired_value_indices: set[int] = set()
    # 用于记录每个标签的最佳配对（含距离 + value box index），供重复标签去重
    best_pairings: dict[str, tuple[str, float, int]] = {}

    for lbl in labels:
        if lbl["matched_label"] in kvp:
            continue  # C0 阶段已处理

        best_vi = -1
        best_dist = float("inf")

        for vi, val in enumerate(values):
            if vi in paired_value_indices:
                continue
            dist = _box_distance(lbl, val, x_weight=0.3)
            # 同 y 行惩罚：值应当与标签有一定 y 落差（表单中值通常在标签下方）
            # 若 dy < 15px，该候选很可能是另一标签或噪声，而非真实值
            if abs(lbl["cy"] - val["cy"]) < 15:
                dist += 60.0
            if dist < best_dist and dist < MAX_PAIR_DISTANCE:
                best_dist = dist
                best_vi = vi

        if best_vi < 0:
            continue

        label_name = lbl["matched_label"]
        # 重复标签去重：保留距离更近的配对
        if label_name in best_pairings and best_dist >= best_pairings[label_name][1]:
            continue
        best_pairings[label_name] = (values[best_vi]["text"], best_dist, best_vi)

    # 将最佳配对写入 kvp，并标记已使用的 value
    for label_name, (val_text, _, best_vi) in best_pairings.items():
        # 检查值是否为标签修饰符（如"大写"/"小写"），
        # 这些文本不应作为字段值
        if _is_label_modifier(val_text, compiled_labels):
            logger.debug(
                f"跳过标签修饰符值 '{val_text}' → label='{label_name}'"
            )
            continue
        kvp[label_name] = val_text
        kvp_bboxes[label_name] = {
            "merged_bbox": _merge_bboxes(
                _box_to_list(values[best_vi]),
                # 找到同名 label box 的坐标
                *[(_box_to_list(lbl)) for lbl in labels if lbl["matched_label"] == label_name]
            )
        }
        paired_value_indices.add(best_vi)

    # ---- D: 表单布局配对（值在上，标签在下） ----
    _pair_form_layout(
        labels, values, compiled_labels, kvp, kvp_bboxes, paired_value_indices
    )

    # ---- E: 同行左右配对（标签左，值右） ----
    _pair_same_row(
        labels, values, compiled_labels, kvp, kvp_bboxes, paired_value_indices
    )

    # 将 bbox 信息注入结果
    kvp["_kvp_bboxes"] = kvp_bboxes
    return kvp


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def extract_kvp_local(
    pil_img,
    lang: str = "ch",
    label_set_name: str = "auto",
) -> dict[str, Any]:
    """使用本地 OCR + 空间规则引擎提取 KVP（离线可用）。

    Args:
        pil_img: PIL Image。
        lang: OCR 语言。
        label_set_name: 标签词典名（"auto" 自动选择 / "deposit_slip" / "invoice" / "generic"）。

    Returns:
        提取的 KVP dict，键为标准化标签名。
    """
    from mineru.utils.custom.labels import auto_select_label_set, get_label_set

    # Step 1: OCR
    logger.info("开始本地 OCR 文本提取...")
    boxes = _ocr_extract_boxes(pil_img, lang=lang)
    logger.debug(f"OCR 检测到 {len(boxes)} 个文本框")

    if not boxes:
        logger.warning("OCR 未检测到任何文本")
        return {}

    # Step 2: 选择标签词典
    if label_set_name == "auto":
        all_text = " ".join(b["text"] for b in boxes)
        selected_name, compiled_labels = auto_select_label_set(all_text)
    else:
        compiled_labels = get_label_set(label_set_name)
        if compiled_labels is None:
            logger.warning(f"未知标签词典 '{label_set_name}'，回退到通用词典")
            compiled_labels = get_label_set("generic") or []
        selected_name = label_set_name

    # Step 3: 配对
    kvp = _pair_kvp(boxes, compiled_labels)

    logger.info(
        f"本地 KVP 提取完成: {len(kvp)} 个字段 "
        f"(词典={selected_name}, OCR boxes={len(boxes)})"
    )
    return kvp
