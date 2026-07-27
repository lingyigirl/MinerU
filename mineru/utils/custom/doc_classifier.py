"""S1 文档分类器（"信号灯"路由）。

根据文档视觉特征和文本特征，将文档分为三类：
- DOCUMENT_PARSE：通用文档（学术/书籍/报告）→ MinerU 三后端
- STRUCTURED_TABLE：密集表格（报表/统计表）→ MinerU Hybrid + OCR 补充
- FORM_KIE：票据/卡证/高密度 KVP 表单 → KIE Pipeline（MLLM）

分类采用视觉特征 + 文本特征的混合策略，不依赖单一模型。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from loguru import logger

from mineru.utils.custom.doc_quality import DocumentQuality
from mineru.utils.pdf_image_tools import (
    DEFAULT_PDF_IMAGE_DPI,
    load_images_from_pdf_core,
)


class DocType(str, Enum):
    """文档类型枚举。

    对应"信号灯"三路路由：
    - DOCUMENT_PARSE（🟢）：通用文档，走现有 MinerU 管线。
    - STRUCTURED_TABLE（🟡）：密集表格，走 Hybrid + OCR 补充。
    - FORM_KIE（🔴）：票据/卡证/KVP 表单，走 KIE Pipeline。
    """

    DOCUMENT_PARSE = "document_parse"
    STRUCTURED_TABLE = "structured_table"
    FORM_KIE = "form_kvp"


# ---------------------------------------------------------------------------
# KVP 关键词词典
# ---------------------------------------------------------------------------

KVP_KEYWORDS: list[str] = [
    # 金融票据
    "户名", "账号", "开户", "存期", "利率", "存款", "取款", "起息日", "到期日",
    "大写", "小写", "币种", "凭证", "存单", "汇款", "支票", "汇票",
    # 证书
    "证书编号", "发证机关", "有效期", "持证人", "注册号",
    # 通用 KVP 特征
    "金额", "合计", "经办", "复核", "签章", "客户号", "流水号",
    # 发票/账单
    "发票代码", "发票号码", "开票日期", "购买方", "销售方", "价税合计",
    "纳税人识别号", "开户行", "账号", "收款人", "付款人", "摘要",
    # 合同/授权书
    "甲方", "乙方", "授权期限", "授权范围", "被授权人", "授权人",
]

# 编译正则表达式以加速匹配（匹配中文字符序列）
_KVP_PATTERN = re.compile("|".join(re.escape(kw) for kw in KVP_KEYWORDS))

# ---------------------------------------------------------------------------
# 表格/表单密度关键词
# ---------------------------------------------------------------------------

TABLE_INDICATOR_KEYWORDS: list[str] = [
    "合计", "总计", "同比", "环比", "增长率", "占比",
    "单位", "数量", "单价", "金额", "金额合计", "余额",
]

_TABLE_KW_PATTERN = re.compile("|".join(re.escape(kw) for kw in TABLE_INDICATOR_KEYWORDS))


def _extract_text_from_page(pil_img) -> str:
    """对单页运行 PaddleOCR 提取文本（用于关键词匹配）。

    Args:
        pil_img: PIL Image 对象。

    Returns:
        提取的文本字符串。
    """
    try:
        from mineru.model.ocr.pytorch_paddle_ocr import PytorchPaddleOCR

        ocr = PytorchPaddleOCR(lang="ch")
        results = ocr.ocr(np.asarray(pil_img))
        if results is None or (isinstance(results, list) and len(results) == 0):
            return ""
        texts = []
        for result in results[0] if results and results[0] else []:
            if result and len(result) > 1 and len(result[1]) > 0:
                texts.append(result[1][0])
        return " ".join(texts)
    except Exception:
        logger.warning("OCR 文本提取失败，回退到纯视觉分类")
        return ""


def _extract_text_sample(pdf_bytes: bytes, sample_pages: list[int], dpi: int) -> str:
    """对采样页运行轻量 OCR 提取文本。

    Args:
        pdf_bytes: PDF 字节流。
        sample_pages: 采样页索引列表。
        dpi: 渲染 DPI。

    Returns:
        所有采样页文本拼接后的字符串。
    """
    try:
        images = load_images_from_pdf_core(pdf_bytes, dpi=dpi)
        all_text = []
        for idx in sample_pages:
            if idx < len(images):
                text = _extract_text_from_page(images[idx]["img_pil"])
                all_text.append(text)
        return " ".join(all_text)
    except Exception:
        logger.exception("文本采样失败")
        return ""


def _count_kvp_keywords(text: str) -> int:
    """统计文本中 KVP 关键词的出现次数。

    Args:
        text: 文本字符串。

    Returns:
        匹配到的关键词个数（去重）。
    """
    if not text:
        return 0
    found = set()
    for match in _KVP_PATTERN.finditer(text):
        found.add(match.group())
    return len(found)


def _count_table_keywords(text: str) -> int:
    """统计文本中表格特征词的出现次数。

    Args:
        text: 文本字符串。

    Returns:
        匹配到的表格关键词个数（去重）。
    """
    if not text:
        return 0
    found = set()
    for match in _TABLE_KW_PATTERN.finditer(text):
        found.add(match.group())
    return len(found)


def _select_sample_pages(page_count: int, max_sample_pages: int = 3) -> list[int]:
    """选择分类分析的采样页索引。"""
    if page_count <= max_sample_pages:
        return list(range(page_count))
    indices = [0, page_count - 1]
    remaining = max_sample_pages - 2
    if remaining > 0 and page_count > 2:
        step = max(1, (page_count - 2) // (remaining + 1))
        for i in range(1, remaining + 1):
            idx = min(i * step, page_count - 2)
            if idx not in indices:
                indices.append(idx)
    return sorted(indices)


def _compute_image_coverage_ratio(pdf_bytes: bytes, sample_indices: list[int], dpi: int) -> float:
    """计算非白色像素占比，用于判断是否以图片/扫描件为主。

    Args:
        pdf_bytes: PDF 字节流。
        sample_indices: 采样页索引。
        dpi: 渲染 DPI。

    Returns:
        非白色像素的平均占比（0.0 ~ 1.0）。
    """
    try:
        import numpy as np

        images = load_images_from_pdf_core(pdf_bytes, dpi=dpi)
        ratios = []
        for idx in sample_indices:
            if idx < len(images):
                np_img = np.asarray(images[idx]["img_pil"].convert("L"))
                non_white = float(np.count_nonzero(np_img < 250) / np_img.size)
                ratios.append(non_white)
        return float(np.mean(ratios)) if ratios else 0.0
    except Exception:
        logger.warning("图片覆盖率计算失败")
        return 0.0


def classify_document(
    pdf_bytes: bytes,
    quality: Optional[DocumentQuality] = None,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,
    max_sample_pages: int = 3,
) -> DocType:
    """对文档进行分类，返回推荐的解析路径（S1 阶段）。

    分类策略（按优先级）：
    1. FORM_KIE：KVP 关键词 ≥ 3 个 且（有印章 或 OCR 难度高）
    2. STRUCTURED_TABLE：表格关键词 ≥ 5 个 且 KVP 关键词 < 3 个
    3. DOCUMENT_PARSE：其他所有文档

    Args:
        pdf_bytes: PDF 文件字节流。
        quality: 预先计算的质量分析结果（可选，传入则复用 S0 结果）。
        dpi: 渲染 DPI。
        max_sample_pages: 最大采样页数。

    Returns:
        文档分类结果（DocType 枚举）。
    """
    try:
        images = load_images_from_pdf_core(pdf_bytes, dpi=dpi)
        page_count = len(images)
        sample_indices = _select_sample_pages(page_count, max_sample_pages)

        # 特征 1: 文本关键词（需要 OCR）
        text_sample = _extract_text_sample(pdf_bytes, sample_indices, dpi)
        kvp_count = _count_kvp_keywords(text_sample)
        table_kw_count = _count_table_keywords(text_sample)

        # 特征 2: 是否有印章（复用 S0 结果或独立计算）
        has_stamp = False
        if quality is not None:
            has_stamp = quality.has_stamp

        # 特征 3: 图片覆盖率
        image_coverage = _compute_image_coverage_ratio(pdf_bytes, sample_indices, dpi)

        # 特征 4: OCR 难度（复用 S0 结果）
        ocr_difficulty = quality.ocr_difficulty if quality else "low"

        logger.debug(
            f"文档分类特征: kvp_count={kvp_count}, table_kw={table_kw_count}, "
            f"stamp={has_stamp}, image_cov={image_coverage:.2%}, "
            f"ocr_diff={ocr_difficulty}, pages={page_count}"
        )

        # 分类规则
        if kvp_count >= 3 and (has_stamp or ocr_difficulty in ("medium", "high")):
            doc_type = DocType.FORM_KIE
        elif kvp_count >= 5 and not has_stamp:
            # 大量 KVP 关键词但无印章 → 可能是纯文本表单
            doc_type = DocType.FORM_KIE
        elif table_kw_count >= 5 and kvp_count < 3:
            doc_type = DocType.STRUCTURED_TABLE
        else:
            doc_type = DocType.DOCUMENT_PARSE

        logger.info(
            f"文档分类结果: {doc_type.value} "
            f"(kvp={kvp_count}, table_kw={table_kw_count}, stamp={has_stamp})"
        )
        return doc_type

    except Exception:
        logger.exception("文档分类失败，回退到通用解析路径")
        return DocType.DOCUMENT_PARSE
