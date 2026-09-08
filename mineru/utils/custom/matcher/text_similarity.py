"""OCR 容错的文本相似度。

用标准库 difflib 实现（零额外依赖），并在数字一致时加分——
票据/表格对齐场景中数字是最强的匹配信号。
"""

from __future__ import annotations

import difflib
import re


def _normalize(text: str) -> str:
    """全角→半角 + 去空白，用于相似度比较前的文本规范化。"""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:  # 全角字符区
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", "", "".join(out))


def _extract_digits(text: str) -> str:
    """提取文本中的全部数字（用于数字一致加分）。"""
    return re.sub(r"[^0-9]", "", text)


def text_similarity(text_a: str, text_b: str) -> float:
    """计算两个文本的 OCR 容错相似度。

    计算逻辑：
    1. 归一化：全角→半角、去空白
    2. 基础分 = difflib.SequenceMatcher 归一化相似度
    3. 数字加分：两者非空数字串完全一致时 +0.2
    4. clamp 到 [0, 1]

    Args:
        text_a, text_b: 待比较的两个文本。

    Returns:
        相似度（0~1，1=完全相同）。

    Example:
        >>> text_similarity("帐号", "账号")  # OCR 形近字
        0.5 左右
        >>> text_similarity("¥700.00", "700.00元")
        0.9 左右
    """
    if not text_a or not text_b:
        return 0.0

    a = _normalize(text_a)
    b = _normalize(text_b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    base = difflib.SequenceMatcher(None, a, b).ratio()

    # 数字加分：两者数字串完全一致（且非空）→ +0.2
    da, db = _extract_digits(a), _extract_digits(b)
    if da and da == db:
        base += 0.2

    return max(0.0, min(1.0, base))
