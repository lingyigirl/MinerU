"""按颜色去除红色印章像素（解析入口的页面图像预处理）。

背景：红色印章（圆章/椭圆章）在版面上会被切成独立图片块。它压在表头或
字段文字上时会造成两类错误：
1. 表格表头格被 VLM 误读（实测：转入金额/借贷标志 → 借出金额/借出金额）；
2. 字段 span 被图片块截断，OCR 只能填回被截断的那一段（实测：本方账号
   开户行丢"支行"、单位丢"单位"、时间范围丢"时间"）。
把印章的红像素白化后，印章不再形成图片块，上述两类错误同时消失；
实测同一文档三次运行逐字节一致。

只处理红色：黑色印章、黑色表格线不受影响（仍走原路径）。

守卫（避免误伤红色正文，如红字金额、红色表格线、红色版头元素）：
1. 先对红掩码做形态学膨胀，把被黑色文字切断的同一印章笔画并成一个整体
   —— 真实印章红环被压在其上的黑字切断，单个连通域只是一段弧
   （实测工行6562 页0 最大连通域 185x90px、宽高比 2.06、占比 0.43%，
   不做合并会漏判真正要去的印章）；
2. 只把"印章状"整体视为可白化区域：bbox 面积占页面比落在
   [min_area_ratio, max_area_ratio]、宽高比接近方形、区域内红像素数足够；
3. 只白化命中区域内的红像素，其余红色内容原样保留。

阈值来自真实样本实测（200 DPI）：
- 工行6562 印章 210x210px，占比 1.14%，宽高比 1.00 → 三页全部命中；
- 威海银行流水两份文档每页头部 256x170px，占比 1.13%，宽高比 1.51 → 命中；
- 新发银行流水页0 的 19252 个红像素分散在正文区（bbox 占比超上限）→ 不命中；
- 人和万德农业银行流水每页 15~103 个红像素（不足 min_red_px）→ 不命中。
"""

from __future__ import annotations

from typing import Iterable

import cv2
import numpy as np
from loguru import logger
from PIL import Image

# 红像素判据：R 明显高于 G/B 且足够亮（浅红抗锯齿像素一并覆盖）
_RED_MIN_CHANNEL = 90
_RED_MIN_CHANNEL_DIFF = 40

# 命中区域至少包含这么多红像素（排除零星红色噪点）
_MIN_RED_PX = 300

# 印章近似方形；细长的红色表格线/整行红底会被排除
_ASPECT_RANGE = (0.5, 2.0)


def build_red_mask(arr: np.ndarray) -> np.ndarray:
    """从 RGB 图像数组生成红像素布尔掩码。

    Args:
        arr: 形如 (H, W, 3) 的 RGB 数组（任意整数 dtype）。

    Returns:
        形如 (H, W) 的布尔数组，True 表示该像素判为红色。
    """
    data = arr.astype(np.int16)
    red = data[..., 0]
    green = data[..., 1]
    blue = data[..., 2]
    return (
        (red - green > _RED_MIN_CHANNEL_DIFF)
        & (red - blue > _RED_MIN_CHANNEL_DIFF)
        & (red > _RED_MIN_CHANNEL)
    )


def select_seal_pixels(
    red_mask: np.ndarray,
    *,
    dilate_px: int,
    min_area_ratio: float,
    max_area_ratio: float,
    min_red_px: int = _MIN_RED_PX,
) -> np.ndarray:
    """从红掩码中挑出"印章状"区域内的像素。

    先膨胀合并笔画，再按 bbox 面积占比/宽高比/红像素数筛选，
    最后只返回命中区域内的红像素（未命中的红色内容不返回）。

    Args:
        red_mask: ``build_red_mask`` 输出的布尔掩码。
        dilate_px: 膨胀核边长（像素），用于跨越印章笔画之间的间隙。
        min_area_ratio: 命中区域 bbox 面积占页面比的下限。
        max_area_ratio: 命中区域 bbox 面积占页面比的上限（防误伤大片红色）。
        min_red_px: 命中区域至少包含的红像素数。

    Returns:
        形如 (H, W) 的布尔数组，True 表示该像素应被白化。
        ``red_mask`` 全为 False 时返回全 False。
    """
    if not red_mask.any():
        return np.zeros_like(red_mask, dtype=bool)

    page_area = red_mask.shape[0] * red_mask.shape[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_px, dilate_px))
    merged = cv2.dilate(red_mask.astype(np.uint8), kernel)

    count, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    selected = np.zeros_like(red_mask, dtype=bool)
    for label in range(1, count):
        x, y, width, height, _ = stats[label]
        region = red_mask[y:y + height, x:x + width]
        red_px = int(region.sum())
        if red_px < min_red_px:
            continue
        area_ratio = (width * height) / page_area
        aspect = width / height if height else 0.0
        if not min_area_ratio <= area_ratio <= max_area_ratio:
            continue
        if not _ASPECT_RANGE[0] <= aspect <= _ASPECT_RANGE[1]:
            continue
        selected[y:y + height, x:x + width] |= region
    return selected


def capture_seal_bboxes(images_list: list[dict]) -> list[list[list[float]]]:
    """在不修改图像的前提下，逐页检测印章 bbox（复用红掩膜+守卫逻辑）。

    与 remove_seal_from_images 使用相同配置阈值。调用时机：白化前，
    用于捕获印章 bbox。白化后这些 bbox 注入 VLM model_list，
    使下游 supplement_vlm_seal_with_ocr 能触发印章文本增强，
    并在最终输出中保留 `<details>seal</detail>` 与印章图块标记。

    Args:
        images_list: 形如 [{"img_pil": PIL.Image}, ...] 的页面图像列表。

    Returns:
        按页组织：[[[x0,y0,x1,y1], ...], ...]，每页一个子列表。
        坐标已在 [0, 1] 范围归一化（÷图像宽高），与 VLM 模型输出一致，
        下游 cal_real_bbox(width×height) 可正确还原。无印章的页返回空列表 []。
    """
    from mineru.utils.custom.config import (
        get_seal_removal_dilate_px,
        get_seal_removal_max_area_ratio,
        get_seal_removal_min_area_ratio,
    )

    dilate_px = get_seal_removal_dilate_px()
    min_area_ratio = get_seal_removal_min_area_ratio()
    max_area_ratio = get_seal_removal_max_area_ratio()
    min_red_px = _MIN_RED_PX

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_px, dilate_px))

    page_bboxes: list[list[list[float]]] = []
    for img_dict in images_list:
        try:
            arr = np.asarray(img_dict["img_pil"].convert("RGB"))
            img_h, img_w = arr.shape[:2]  # 像素尺寸，用于归一化
            red_mask = build_red_mask(arr)

            if not red_mask.any():
                page_bboxes.append([])
                continue

            page_area = red_mask.shape[0] * red_mask.shape[1]
            merged = cv2.dilate(red_mask.astype(np.uint8), kernel)
            _, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)

            bboxes = []
            for label in range(1, stats.shape[0]):
                x, y, width, height, _ = stats[label]
                region = red_mask[y : y + height, x : x + width]
                red_px = int(region.sum())
                if red_px < min_red_px:
                    continue
                area_ratio = (width * height) / page_area
                if not min_area_ratio <= area_ratio <= max_area_ratio:
                    continue
                aspect = width / height if height else 0.0
                if not _ASPECT_RANGE[0] <= aspect <= _ASPECT_RANGE[1]:
                    continue
                # 输出归一化坐标 [0, 1]（下游 cal_real_bbox 用 width/height 还原）
                bboxes.append([
                    float(x) / img_w,
                    float(y) / img_h,
                    float(x + width) / img_w,
                    float(y + height) / img_h,
                ])
            page_bboxes.append(bboxes)
        except Exception:
            page_bboxes.append([])
    return page_bboxes


def remove_seal_pixels(
    img: Image.Image,
    *,
    dilate_px: int,
    min_area_ratio: float,
    max_area_ratio: float,
) -> tuple[Image.Image, int]:
    """把单页图像里的红色印章像素白化。

    Args:
        img: 页面 PIL 图像（RGB 或灰度均可，内部按 RGB 处理）。
        dilate_px: 膨胀核边长（像素）。
        min_area_ratio: 命中区域 bbox 面积占页面比的下限。
        max_area_ratio: 命中区域 bbox 面积占页面比的上限。

    Returns:
        (处理后图像, 白化像素数)。未命中印章时返回 (原图, 0)。
    """
    arr = np.asarray(img.convert("RGB"))
    seal = select_seal_pixels(
        build_red_mask(arr),
        dilate_px=dilate_px,
        min_area_ratio=min_area_ratio,
        max_area_ratio=max_area_ratio,
    )
    removed = int(seal.sum())
    if removed == 0:
        return img, 0
    out = arr.copy()
    out[seal] = 255
    return Image.fromarray(out), removed


def remove_seal_from_images(images_list: Iterable[dict]) -> int:
    """对页面图像列表逐页去除红色印章（原地替换 ``img_pil``）。

    阈值与开关取自自定义配置（``MINERU_SEAL_REMOVAL`` 等），
    任何一页处理失败只记录警告、不影响其它页面。

    Args:
        images_list: 形如 [{"img_pil": PIL.Image}, ...] 的页面图像列表。

    Returns:
        白化的红像素总数。
    """
    from mineru.utils.custom.config import (
        get_seal_removal_dilate_px,
        get_seal_removal_enable,
        get_seal_removal_max_area_ratio,
        get_seal_removal_min_area_ratio,
    )

    if not get_seal_removal_enable():
        return 0

    dilate_px = get_seal_removal_dilate_px()
    min_area_ratio = get_seal_removal_min_area_ratio()
    max_area_ratio = get_seal_removal_max_area_ratio()
    total = 0
    for idx, img_dict in enumerate(images_list):
        try:
            img_dict["img_pil"], removed = remove_seal_pixels(
                img_dict["img_pil"],
                dilate_px=dilate_px,
                min_area_ratio=min_area_ratio,
                max_area_ratio=max_area_ratio,
            )
            total += removed
        except Exception as exc:
            logger.warning(f"印章去除——第{idx}页处理失败: {exc}")
    if total:
        logger.info(
            f"印章去除——共白化 {total} 个红色像素"
            f"（膨胀={dilate_px}px，面积占比∈[{min_area_ratio:.3f}, {max_area_ratio:.3f}]）"
        )
    return total
