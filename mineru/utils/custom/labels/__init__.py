"""KVP 标签词典注册表。

提供标签词典的注册、查询和自动匹配功能。
每种文档类型对应一个词典模块，由 KVP 引擎根据 OCR 提取到的文本自动选择最佳匹配。
"""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger

# ---------------------------------------------------------------------------
# 词典注册表
# ---------------------------------------------------------------------------

from mineru.utils.custom.labels.deposit_slip import DEPOSIT_SLIP_LABELS
from mineru.utils.custom.labels.bank_receipt import BANK_RECEIPT_LABELS
from mineru.utils.custom.labels.invoice import INVOICE_LABELS
from mineru.utils.custom.labels.generic import GENERIC_LABELS

# (词典名, 标签列表, 用于自动匹配的关键特征词)
LABEL_SETS: list[tuple[str, list[tuple[str, str]], list[str]]] = [
    (
        "bank_receipt",
        BANK_RECEIPT_LABELS,
        [
            "回单", "电子回单", "业务流水号", "交易金额", "收费金额",
            "付款人", "收款人", "附言", "交易时间", "记账日期",
        ],
    ),
    (
        "deposit_slip",
        DEPOSIT_SLIP_LABELS,
        ["存单", "存期", "起息日", "开户日", "整存整取", "存款"],
    ),
    (
        "invoice",
        INVOICE_LABELS,
        ["发票代码", "发票号码", "购买方", "销售方", "价税合计", "开票日期"],
    ),
    (
        "generic",
        GENERIC_LABELS,
        [],  # 通用词典总是可以匹配
    ),
]

# 预编译每个词典的正则
_COMPILED_SETS: list[tuple[str, list[tuple[re.Pattern, str]], list[str]]] = []
for _name, _labels, _keywords in LABEL_SETS:
    _compiled = [(re.compile(p), label) for p, label in _labels]
    _COMPILED_SETS.append((_name, _compiled, _keywords))


def list_label_sets() -> list[str]:
    """列出所有可用的标签词典名。"""
    return [name for name, _, _ in LABEL_SETS]


def auto_select_label_set(ocr_text_sample: str) -> tuple[str, list[tuple[re.Pattern, str]]]:
    """根据 OCR 提取的文本样本自动选择最匹配的标签词典。

    匹配策略：统计每个词典的特征关键词命中数，取最高分。
    通用词典作为兜底。

    Args:
        ocr_text_sample: OCR 提取的文本样本（用于关键词匹配）。

    Returns:
        (词典名, [(编译正则, 标准标签), ...]) 元组。
    """
    if not ocr_text_sample:
        # 无文本 → 通用词典
        for name, compiled, _ in _COMPILED_SETS:
            if name == "generic":
                logger.debug(f"无 OCR 文本，使用通用标签词典")
                return name, compiled

    best_name = "generic"
    best_compiled = None
    best_score = 0

    for name, compiled, keywords in _COMPILED_SETS:
        if name == "generic":
            if best_compiled is None:
                best_compiled = compiled
            continue

        # 统计特征关键词命中
        score = sum(1 for kw in keywords if kw in ocr_text_sample)
        if score > best_score:
            best_score = score
            best_name = name
            best_compiled = compiled
            logger.debug(f"词典 '{name}' 命中 {score}/{len(keywords)} 个特征词")

    if best_compiled is None:
        # fallback: 取第一个（generic）
        best_compiled = _COMPILED_SETS[0][1]

    logger.info(f"自动选择标签词典: {best_name} (score={best_score})")
    return best_name, best_compiled


def get_label_set(name: str) -> Optional[list[tuple[re.Pattern, str]]]:
    """按名称获取指定标签词典。

    Args:
        name: 词典名（"deposit_slip" / "invoice" / "generic"）。

    Returns:
        编译后的标签列表，不存在则返回 None。
    """
    for set_name, compiled, _ in _COMPILED_SETS:
        if set_name == name:
            return compiled
    return None
