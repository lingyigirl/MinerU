"""S1 文档分类器（"信号灯"路由）。

根据文档视觉特征和文本特征，将文档分为三类：
- DOCUMENT_PARSE：通用文档（学术/书籍/报告）→ MinerU 三后端
- STRUCTURED_TABLE：密集表格（报表/统计表）→ MinerU Hybrid + OCR 补充
- FORM_KVP：票据/卡证/高密度 KVP 表单 → KVP Pipeline（MLLM）

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


# ---------------------------------------------------------------------------
# PaddleOCR 懒加载单例（避免每次请求重建模型）
# ---------------------------------------------------------------------------

_ocr_instance = None


def get_ocr():
    """获取 PaddleOCR 单例（线程安全由 GIL 保证）。

    首次调用加载模型（~2s），后续调用直接复用（~50ms）。
    KVP 本地引擎也通过此函数复用该实例，避免重复初始化。
    """
    global _ocr_instance
    if _ocr_instance is None:
        from mineru.model.ocr.pytorch_paddle import PytorchPaddleOCR

        _ocr_instance = PytorchPaddleOCR(lang="ch")
        logger.info("PaddleOCR 单例已初始化")
    return _ocr_instance


# ---------------------------------------------------------------------------
# PyMuPDF 快速文本提取（优先于 OCR）
# ---------------------------------------------------------------------------

def _extract_text_fast(pdf_bytes: bytes, max_pages: int = 2) -> str:
    """使用 PyMuPDF 快速提取嵌入文本（无需 OCR）。

    对于有嵌入文本层的 PDF（大多数学术/办公文档），可在 ~10ms 内
    获取足量文本用于关键词分类，完全跳过 OCR。

    Args:
        pdf_bytes: PDF 字节流。
        max_pages: 最多提取页数。

    Returns:
        提取的文本字符串，如果无嵌入文本则返回空字符串。
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texts = []
        for i in range(min(max_pages, doc.page_count)):
            page_text = doc[i].get_text()
            if page_text:
                texts.append(page_text)
        doc.close()
        text = " ".join(texts).strip()
        if len(text) >= 50:  # 有足够的嵌入文本，直接使用
            logger.debug(f"PyMuPDF 快速提取文本: {len(text)} 字符 ({max_pages} 页)")
            return text
    except Exception:
        logger.debug("PyMuPDF 文本提取失败，回退到 OCR")
    return ""


class DocType(str, Enum):
    """文档类型枚举。

    对应"信号灯"三路路由：
    - DOCUMENT_PARSE（🟢）：通用文档，走现有 MinerU 管线。
    - STRUCTURED_TABLE（🟡）：密集表格，走 Hybrid + OCR 补充。
    - FORM_KVP（🔴）：票据/卡证/KVP 表单，走 KVP Pipeline。
    """

    DOCUMENT_PARSE = "document_parse"
    STRUCTURED_TABLE = "structured_table"
    FORM_KVP = "form_kvp"


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
    # 银行回单
    "回单", "交易时间", "交易金额", "收费金额", "附言", "记账日期",
    "业务流水号", "业务类型",
]

# 编译正则表达式以加速匹配（匹配中文字符序列）
_KVP_PATTERN = re.compile("|".join(re.escape(kw) for kw in KVP_KEYWORDS))

# ---------------------------------------------------------------------------
# 表格/表单密度关键词
# ---------------------------------------------------------------------------

TABLE_INDICATOR_KEYWORDS: list[str] = [
    "合计", "总计", "同比", "环比", "增长率", "占比",
    "单位", "数量", "单价", "金额", "金额合计", "余额",
    "交易金额", "收费金额",
    # 金融数据表格常见术语（信用报告、对账单、交易明细等）
    "账户", "交易日期", "借方", "贷方",
    "期初余额", "期末余额", "序号", "备注",
]

_TABLE_KW_PATTERN = re.compile("|".join(re.escape(kw) for kw in TABLE_INDICATOR_KEYWORDS))


def _extract_text_from_page(pil_img) -> str:
    """对单页运行 PaddleOCR 提取文本（用于关键词匹配）。

    复用模块级 OCR 单例，避免每次请求重建模型。

    Args:
        pil_img: PIL Image 对象。

    Returns:
        提取的文本字符串。
    """
    try:
        import numpy as np

        ocr = get_ocr()  # 复用单例
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


def _extract_text_sample(
    pdf_bytes: bytes,
    preview_images: list[dict],
    max_ocr_pages: int = 2,
) -> str:
    """从 PDF 提取文本样本用于分类（PyMuPDF 优先，OCR 兜底）。

    Args:
        pdf_bytes: PDF 字节流。
        preview_images: 已渲染的前 N 页图片列表（供 OCR 兜底使用）。
        max_ocr_pages: OCR 兜底时最多处理的页数。

    Returns:
        所有提取文本拼接后的字符串。
    """
    # 第 1 步：尝试 PyMuPDF 快速提取（~10ms，适用于有嵌入文本的 PDF）
    try:
        fast_text = _extract_text_fast(pdf_bytes, max_pages=max_ocr_pages)
        if fast_text and len(fast_text) >= 50:
            return fast_text
    except Exception:
        logger.debug("PyMuPDF 快速提取跳过")

    # 第 2 步：OCR 兜底（仅对扫描件，使用已渲染的预览图片）
    try:
        all_text = []
        for idx in range(min(max_ocr_pages, len(preview_images))):
            text = _extract_text_from_page(preview_images[idx]["img_pil"])
            all_text.append(text)
        return " ".join(all_text)
    except Exception:
        logger.exception("OCR 文本采样失败")
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


def _count_repeating_kvp_labels(text: str) -> int:
    """统计 OCR 文本中出现 ≥2 次的 KVP 标签数量。

    银行回单等表格型票据的核心特征是同一标签对应多个实体
    （如付款人户名 vs 收款人户名），导致 KVP 标签重复出现。
    这种重复标签是表格结构的强信号——简单表单中每个标签通常只出现一次。

    Args:
        text: OCR 提取的文本字符串。

    Returns:
        重复出现的 KVP 标签数量（出现次数 ≥ 2 的标签个数）。
    """
    if not text:
        return 0
    from collections import Counter

    matches = _KVP_PATTERN.findall(text)
    counter = Counter(matches)
    return sum(1 for count in counter.values() if count >= 2)


def _has_repeating_labels(text: str) -> bool:
    """检测 OCR 文本中是否存在重复的标签模式（表格结构特征）。

    表格型文档（如银行回单、对账单）的 OCR 输出中，同一行会出现多个
    相同标签（如 "户名...账号...户名...账号"），这是表格网格布局的标志。
    简单 KVP 表单中每个标签通常只出现一次。

    Args:
        text: OCR 提取的文本字符串。

    Returns:
        True 如果检测到重复标签模式。
    """
    if not text:
        return False
    # 在同一行中检测重复的关键标签（表格网格特征）
    repeating_patterns = [
        r"户名.*户名",  # 两列表格：付款人户名 + 收款人户名
        r"账号.*账号",  # 两列表格：付款人账号 + 收款人账号
        r"开户行.*开户行",  # 两列表格：付款人开户行 + 收款人开户行
        r"金额.*金额",  # 多金额字段（交易金额 + 收费金额）
    ]
    for p in repeating_patterns:
        if re.search(p, text):
            return True
    return False


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


def _compute_image_coverage_ratio(
    preview_images: list[dict],
    sample_indices: list[int],
) -> float:
    """计算非白色像素占比，用于判断是否以图片/扫描件为主。

    Args:
        preview_images: 已渲染的预览图片列表。
        sample_indices: 采样页索引。

    Returns:
        非白色像素的平均占比（0.0 ~ 1.0）。
    """
    try:
        import numpy as np

        ratios = []
        for idx in sample_indices:
            if idx < len(preview_images):
                np_img = np.asarray(preview_images[idx]["img_pil"].convert("L"))
                non_white = float(np.count_nonzero(np_img < 250) / np_img.size)
                ratios.append(non_white)
        return float(np.mean(ratios)) if ratios else 0.0
    except Exception:
        logger.warning("图片覆盖率计算失败")
        return 0.0


def classify_document(
    pdf_bytes: bytes,
    quality: Optional[DocumentQuality] = None,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,  # 分类使用 DEFAULT_PDF_IMAGE_DPI，通过环境变量 MINERU_PDF_RENDER_DPI 控制
    max_preview_pages: int = 2,  # 只渲染前 N 页用于分类
) -> DocType:
    """对文档进行快速分类，返回推荐的解析路径（S1 阶段）。

    分类流程（性能优先）：
    1. PyMuPDF 获取总页数（不渲染）
    2. 只渲染前 max_preview_pages 页用于视觉分析
    3. PyMuPDF 快速提取嵌入文本 → 关键词匹配
    4. 无嵌入文本时回退到 PaddleOCR（复用单例）

    分类策略（按优先级）：
    1. DOCUMENT_PARSE：重复标签模式（同一行出现 "户名...户名" 等表格结构）
    2. DOCUMENT_PARSE：重复 KVP 标签 ≥ 2 个 + KVP≥5 + 有印章
       （银行回单等表格型票据，标签重复出现说明有二维结构，不限页数）
    3. FORM_KVP：KVP 关键词 ≥ 3 个 且（有印章 或 OCR 难度高）
    4. DOCUMENT_PARSE：KVP≥5 + 表格关键词≥3 + 无印章（发票含货物清单）
    5. FORM_KVP：KVP 关键词 ≥ 5 个 且 无印章 且 短文档（≤2 页）
       （多页文档虽 KVP 术语密集但通常是金融报告/对账单，走 DOCUMENT_PARSE）
    6. STRUCTURED_TABLE：表格关键词 ≥ 5 个 且 KVP 关键词 < 3 个
    7. DOCUMENT_PARSE：其他所有文档

    Args:
        pdf_bytes: PDF 文件字节流。
        quality: 预先计算的质量分析结果（可选，传入则复用 S0 结果）。
        dpi: 渲染 DPI，默认 200（与 DEFAULT_PDF_IMAGE_DPI 对齐）。
        max_preview_pages: 最多渲染页数。

    Returns:
        文档分类结果（DocType 枚举）。
    """
    try:
        # 获取总页数（不渲染，从 PDF 元数据获取）
        page_count = 0
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = doc.page_count
            doc.close()
        except Exception:
            pass

        # 只渲染前 N 页用于视觉分析和 OCR 兜底
        end_page = max_preview_pages - 1 if max_preview_pages > 0 else None
        preview_images = load_images_from_pdf_core(
            pdf_bytes, dpi=dpi, start_page_id=0, end_page_id=end_page,
        )
        if not preview_images:
            logger.warning("PDF 渲染后无有效页面，回退到通用解析")
            return DocType.DOCUMENT_PARSE

        if page_count == 0:
            page_count = len(preview_images)

        sample_indices = _select_sample_pages(len(preview_images), max_preview_pages)

        # 特征 1: 文本关键词（PyMuPDF 优先，OCR 兜底）
        text_sample = _extract_text_sample(pdf_bytes, preview_images, max_ocr_pages=max_preview_pages)
        kvp_count = _count_kvp_keywords(text_sample)
        table_kw_count = _count_table_keywords(text_sample)
        repeating_kvp_count = _count_repeating_kvp_labels(text_sample)

        # 特征 2: 是否有印章（复用 S0 结果或独立计算）
        # 单页/少页文档降低印章阈值：单页票据 1.5% 红章已是明显信号
        has_stamp = False
        if quality is not None:
            if page_count <= 2:
                has_stamp = quality.stamp_ratio > 0.005  # 0.5%，适配单页票据
            else:
                has_stamp = quality.has_stamp  # 多页文档保持原阈值

        # 特征 3: 图片覆盖率（复用已渲染的预览图片）
        image_coverage = _compute_image_coverage_ratio(preview_images, sample_indices)

        # 特征 4: OCR 难度（复用 S0 结果）
        ocr_difficulty = quality.ocr_difficulty if quality else "low"

        logger.debug(
            f"文档分类特征: kvp_count={kvp_count}, table_kw={table_kw_count}, "
            f"repeating_kvp={repeating_kvp_count}, "
            f"stamp={has_stamp}, image_cov={image_coverage:.2%}, "
            f"ocr_diff={ocr_difficulty}, pages={page_count}"
        )

        # OCR 不可用时的纯视觉 fallback 分类
        if not text_sample or len(text_sample) < 10:
            logger.info("OCR 文本不可用，启用纯视觉特征 fallback 分类")
            if page_count <= 2 and (has_stamp or image_coverage > 0.30):
                doc_type = DocType.FORM_KVP  # 短文档 + 印章/高覆盖率 → 票据
            elif page_count == 1 and image_coverage > 0.15:
                doc_type = DocType.FORM_KVP  # 单页 + 中等覆盖率 → 可能表单
            elif table_kw_count >= 5:
                doc_type = DocType.STRUCTURED_TABLE
            else:
                doc_type = DocType.DOCUMENT_PARSE
        else:
            # OCR 可用时的标准分类规则
            # 优先检测表格 + KVP 混合结构（如银行回单、对账单）
            # 这类文档有重复标签模式（同一行出现 "户名...账号...户名...账号"）
            # 应走 Hybrid 管线由 VLM 处理表格，而非 KVP 本地引擎
            has_repeating = _has_repeating_labels(text_sample)
            if has_repeating and kvp_count >= 3 and table_kw_count >= 3:
                # 表格型 KVP 文档（银行回单、对账单等）→ Hybrid 处理表格
                logger.info(
                    f"检测到表格+KVP混合结构（重复标签模式），"
                    f"路由到通用解析（Hybrid后端处理表格）"
                )
                doc_type = DocType.DOCUMENT_PARSE
            elif (kvp_count >= 5 and repeating_kvp_count >= 2
                  and has_stamp):
                # 重复标签型 KVP 文档（银行回单、对账单等）
                # 虽有印章但 KVP 标签重复出现（如付款人户名 vs 收款人户名），
                # 说明存在二维表格结构，KVP 本地引擎无法处理合并单元格
                # 走 Hybrid 后端由 VLM 做表格结构识别
                # 注意：不再限制 page_count <= 2，多页文档中重复标签同样是表格强信号
                logger.info(
                    f"检测到表格型 KVP 文档（重复KVP标签="
                    f"{repeating_kvp_count}），虽有印章但路由到"
                    f"通用解析（Hybrid 后端处理表格）"
                )
                doc_type = DocType.DOCUMENT_PARSE
            elif kvp_count >= 3 and (has_stamp or ocr_difficulty in ("medium", "high")):
                doc_type = DocType.FORM_KVP
            elif kvp_count >= 5 and table_kw_count >= 3 and not has_stamp:
                # 含表格结构的票据（如增值税发票含货物清单）
                # KVP 本地引擎无法处理多行表格（标签只能匹配一次）
                # 走 Hybrid 后端由 VLM 做表格结构识别
                logger.info(
                    f"检测到表格型 KVP 文档（table_kw={table_kw_count}），"
                    f"路由到通用解析（Hybrid 后端处理表格）"
                )
                doc_type = DocType.DOCUMENT_PARSE
            elif kvp_count >= 5 and not has_stamp and page_count <= 2:
                # 短文档 + KVP 关键词多 + 无印章 → 表单（存单、回单等）
                # 多页文档（≥3 页）不应仅凭 KVP 关键词就判定为表单，
                # 金融报告、对账单等虽术语密集但本质是表格报告，走 DOCUMENT_PARSE
                doc_type = DocType.FORM_KVP
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
