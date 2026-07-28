"""S0 文档质量分析器。

在分类和解析之前对 PDF 进行质量评估，结果可用于：
- 影响路由决策（低质量扫描件更适合走 OCR-KVP 管线）
- 触发预处理（旋转修正、去模糊）
- 作为下游解析引擎的提示信息

质量维度：
- 分辨率 / DPI
- 模糊程度（拉普拉斯方差）
- 印章/水印检测
- 旋转/倾斜检测
- 手写文字比例
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from loguru import logger

from mineru.utils.pdf_image_tools import (
    DEFAULT_PDF_IMAGE_DPI,
    load_images_from_pdf_core,
)


@dataclass
class DocumentQuality:
    """文档质量评估结果。

    Attributes:
        quality_score: 综合质量分（0.0 ~ 1.0），越高越好。
        dpi: 渲染 DPI（基于 PDF 页面尺寸推算）。
        is_blurry: 是否检测到模糊。
        blur_score: 模糊程度（拉普拉斯方差），值越小越模糊。None 表示未计算。
        has_stamp: 是否检测到印章/签章。
        stamp_ratio: 印章像素占页面比例（0.0 ~ 1.0）。
        has_watermark: 是否检测到水印。
        has_rotation: 是否有明显的旋转/倾斜。
        handwriting_ratio: 手写文字区域占比（0.0 ~ 1.0），暂未实现。
        ocr_difficulty: OCR 难度评级："low" | "medium" | "high"。
        page_count: 总页数。
        sample_pages_analyzed: 实际分析过的采样页数。
    """

    quality_score: float = 1.0
    dpi: int = DEFAULT_PDF_IMAGE_DPI
    is_blurry: bool = False
    blur_score: Optional[float] = None
    has_stamp: bool = False
    stamp_ratio: float = 0.0
    has_watermark: bool = False
    has_rotation: bool = False
    handwriting_ratio: float = 0.0
    ocr_difficulty: str = "low"
    page_count: int = 0
    sample_pages_analyzed: int = 0

    _warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """生成人类可读的质量摘要。"""
        parts = [f"质量分={self.quality_score:.2f}", f"OCR难度={self.ocr_difficulty}"]
        if self.is_blurry:
            parts.append("模糊")
        if self.has_stamp:
            parts.append(f"印章({self.stamp_ratio:.1%})")
        if self.has_watermark:
            parts.append("水印")
        if self.has_rotation:
            parts.append("旋转")
        if self._warnings:
            parts.append(f"警告:{len(self._warnings)}条")
        return f"DocumentQuality({', '.join(parts)})"


def _compute_blur_score(pil_img) -> float:
    """用拉普拉斯方差估计图片清晰度。

    Args:
        pil_img: PIL Image 对象。

    Returns:
        拉普拉斯方差值，越大越清晰。通常 < 100 表示模糊。
    """
    import cv2

    gray = cv2.cvtColor(np.asarray(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _detect_stamp_ratio(pil_img) -> float:
    """检测红色印章像素占比。

    在 HSV 空间检测红色区域（印章、签章），返回红色像素占总像素的比例。

    Args:
        pil_img: PIL Image 对象。

    Returns:
        红色像素占比（0.0 ~ 1.0）。
    """
    import cv2

    np_img = np.asarray(pil_img.convert("RGB"))
    hsv = cv2.cvtColor(np_img, cv2.COLOR_RGB2HSV)

    # 红色在 HSV 中的两个区间（跨越 0°/180° 边界）
    lower_red_1 = np.array([0, 50, 50])
    upper_red_1 = np.array([10, 255, 255])
    lower_red_2 = np.array([156, 50, 50])
    upper_red_2 = np.array([180, 255, 255])

    mask1 = cv2.inRange(hsv, lower_red_1, upper_red_1)
    mask2 = cv2.inRange(hsv, lower_red_2, upper_red_2)
    red_mask = mask1 | mask2

    total_pixels = red_mask.size
    red_pixels = int(np.count_nonzero(red_mask))
    return red_pixels / total_pixels if total_pixels > 0 else 0.0


def _estimate_dpi_from_page(pil_img, pdf_page_size) -> int:
    """根据 PDF 页面尺寸和渲染图片尺寸估算实际 DPI。

    Args:
        pil_img: 渲染后的 PIL Image。
        pdf_page_size: PDF 页面尺寸（像素，基于 72 DPI）。

    Returns:
        估算的 DPI 值。
    """
    w, h = pil_img.size
    pdf_w, pdf_h = pdf_page_size
    if pdf_w <= 0 or pdf_h <= 0:
        return DEFAULT_PDF_IMAGE_DPI
    scale_x = w / pdf_w
    scale_y = h / pdf_h
    return int(round(min(scale_x, scale_y) * 72))


def _select_sample_pages(page_count: int, max_sample_pages: int = 3) -> list[int]:
    """选择需要分析的采样页索引。

    策略：首尾页必选，中间均匀分布。

    Args:
        page_count: 总页数。
        max_sample_pages: 最大采样页数。

    Returns:
        采样页索引列表。
    """
    if page_count <= max_sample_pages:
        return list(range(page_count))

    indices = [0, page_count - 1]  # 首页 + 尾页
    remaining = max_sample_pages - 2
    if remaining > 0 and page_count > 2:
        step = max(1, (page_count - 2) // (remaining + 1))
        for i in range(1, remaining + 1):
            idx = min(i * step, page_count - 2)
            if idx not in indices:
                indices.append(idx)
    return sorted(indices)


def analyze_document_quality(
    pdf_bytes: bytes,
    dpi: int = 100,  # 质量分析用 100 DPI 足够，速度优先
    max_pages: int = 2,  # 只渲染前 N 页做质量分析，无需全量渲染
    max_sample_pages: int = 2,
    blur_threshold: float = 100.0,
    stamp_threshold: float = 0.02,
) -> DocumentQuality:
    """对 PDF 文档进行快速质量分析（S0 阶段）。

    只渲染前 max_pages 页，检测清晰度、印章等关键质量指标。

    Args:
        pdf_bytes: PDF 文件字节流。
        dpi: 渲染 DPI，默认 100（质量分析对分辨率不敏感，速度优先）。
        max_pages: 最多渲染页数，0 表示全部页。默认 2 页。
        max_sample_pages: 最大采样页数。
        blur_threshold: 模糊判定阈值（拉普拉斯方差），低于此值判定为模糊。
        stamp_threshold: 印章判定阈值，红色像素占比超过此值判定为有印章。

    Returns:
        DocumentQuality 对象，包含完整的质量评估结果。
    """
    quality = DocumentQuality(dpi=dpi)

    try:
        # 只渲染前 max_pages 页（质量分析不需要全量渲染）
        end_page = max_pages - 1 if max_pages > 0 else None
        all_images = load_images_from_pdf_core(
            pdf_bytes, dpi=dpi, start_page_id=0, end_page_id=end_page,
        )
        quality.page_count = len(all_images)

        # 用 pymupdf 获取真实总页数（不额外渲染）
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            quality.page_count = doc.page_count
            doc.close()
        except Exception:
            pass  # 回退到已渲染的页数

        sample_indices = _select_sample_pages(len(all_images), max_sample_pages)
        quality.sample_pages_analyzed = len(sample_indices)

        blur_scores = []
        stamp_ratios = []

        for idx in sample_indices:
            pil_img = all_images[idx]["img_pil"]

            # 模糊检测
            try:
                blur_score = _compute_blur_score(pil_img)
                blur_scores.append(blur_score)
            except Exception:
                logger.warning(f"模糊检测失败 (page {idx})")

            # 印章检测
            try:
                sr = _detect_stamp_ratio(pil_img)
                stamp_ratios.append(sr)
            except Exception:
                logger.warning(f"印章检测失败 (page {idx})")

        # 汇总模糊结果
        if blur_scores:
            quality.blur_score = float(np.mean(blur_scores))
            quality.is_blurry = quality.blur_score < blur_threshold

        # 汇总印章结果
        if stamp_ratios:
            quality.stamp_ratio = float(np.mean(stamp_ratios))
            quality.has_stamp = quality.stamp_ratio > stamp_threshold

        # 计算综合质量分
        quality.quality_score = _compute_quality_score(quality)

        # 判定 OCR 难度
        quality.ocr_difficulty = _classify_ocr_difficulty(quality)

    except Exception:
        logger.exception("文档质量分析失败，返回默认质量评估")

    logger.debug(f"文档质量分析结果: {quality.summary()}")
    return quality


def _compute_quality_score(q: DocumentQuality) -> float:
    """综合各维度计算质量分（0.0 ~ 1.0）。

    Args:
        q: DocumentQuality 对象（已填充各维度指标）。

    Returns:
        综合质量分。1.0 为最佳。
    """
    score = 1.0

    # 模糊惩罚
    if q.is_blurry and q.blur_score is not None:
        blur_factor = min(1.0, q.blur_score / 200.0)
        score *= 0.5 + 0.5 * blur_factor

    # 印章惩罚（印章区域文字难以识别）
    if q.has_stamp:
        score *= 0.7

    # 手写惩罚（暂未实现手写检测，预留）
    if q.handwriting_ratio > 0.1:
        score *= 0.8

    return max(0.0, min(1.0, score))


def _classify_ocr_difficulty(q: DocumentQuality) -> str:
    """根据质量指标判定 OCR 难度。

    Args:
        q: DocumentQuality 对象。

    Returns:
        "low" | "medium" | "high"
    """
    difficulty_score = 0
    if q.is_blurry:
        difficulty_score += 1
    if q.has_stamp:
        difficulty_score += 1
    if q.handwriting_ratio > 0.1:
        difficulty_score += 1

    if difficulty_score == 0:
        return "low"
    elif difficulty_score == 1:
        return "medium"
    else:
        return "high"
