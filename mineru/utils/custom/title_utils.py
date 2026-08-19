"""标题救援模块。

在 Hybrid 模式下，VLM 对同一类元素分类不稳定：同一份审计报告里
「利润表」被判为 paragraph_title、「现金流量表/所有者权益变动表」被判为
table_caption，均被保留；而「资产负债表」却被判为 header，被 hybrid 转换
（hybrid_magic_model.py）统一归入 discarded_blocks 丢弃，导致 md 缺标题。

本模块在 Hybrid 模式的后处理阶段，识别「水平居中 + 单行 + 短文本 + 跨页
不重复」的 header 块，改判为 title 并移回 preproc_blocks，恢复丢失的标题。

[自定义] 此模块是 Fork 项目新增的自定义代码，上游无此模块。
合并上游时无需关注此文件，但需保留 hook 调用点。
"""

import re
from collections import Counter

from mineru.utils.enum_class import BlockType

# 标题文本长度区间（字）：太短（单字）通常是噪声/序号，太长（>20 字）通常是
# 段落或页眉说明文字，均不作为标题救援对象。
# 20 字上限需覆盖「2023年12月份会计报表」（13 字）、
# 「个人账户交易明细清单（工资卡专用）」（17 字）等标准报表标题。
_MIN_TITLE_LEN = 2
_MAX_TITLE_LEN = 20

# 水平居中容差：bbox 中心偏离页面中心的比例上限（页宽比例）。页眉通常靠左/靠右，
# 居中且短小的块更可能是标题。
_CENTERED_TOLERANCE = 0.12


def _block_text(block: dict) -> str:
    """提取块内全部 span 文本（按 lines→spans 与顶层 spans 两处拼接）。

    Args:
        block: middle.json 中的一个块字典。

    Returns:
        拼接后的纯文本。
    """
    parts: list[str] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            content = span.get("content")
            if content:
                parts.append(content)
    for span in block.get("spans", []):
        content = span.get("content")
        if content:
            parts.append(content)
    return "".join(parts)


def rescue_discarded_title_headers(pdf_info_list: list) -> None:
    """将被误判为 header 的居中短标题从 discarded_blocks 救回 preproc_blocks。

    VLM 对同一类元素分类不稳定：同一份审计报告里「利润表」被判为 paragraph_title、
    「资产负债表」却被判为 header（被 hybrid 转换丢弃）。本函数识别「水平居中 +
    单行 + 短文本 + 跨页不重复」的 header 块，改判为 title 并移回正文，恢复丢失的标题。

    Args:
        pdf_info_list: middle.json 的 pdf_info 列表（就地修改）。
    """
    # 统计跨页 header 文本出现次数，识别每页重复出现的 running header（页眉）
    text_counts: Counter = Counter()
    for page_info in pdf_info_list:
        for block in page_info.get("discarded_blocks", []):
            if block.get("type") == BlockType.HEADER:
                text = _block_text(block).strip()
                if text:
                    text_counts[text] += 1

    for page_info in pdf_info_list:
        page_size = page_info.get("page_size") or [1, 1]
        page_w = page_size[0] if page_size[0] else 1
        preproc = page_info.setdefault("preproc_blocks", [])
        discarded = page_info.get("discarded_blocks", [])
        rescued: list[dict] = []
        for block in discarded:
            if block.get("type") != BlockType.HEADER:
                continue
            text = _block_text(block).strip()
            # 短标题：长度在区间内，且非纯数字（排除页码）
            if not (_MIN_TITLE_LEN <= len(text) <= _MAX_TITLE_LEN) or text.isdigit():
                continue
            # 单行（页眉/页脚多为多行）
            if len(block.get("lines") or []) != 1:
                continue
            # 水平居中：bbox 中心偏离页面中心不超过容差
            x1, _, x2, _ = block.get("bbox") or [0, 0, 0, 0]
            if abs((x1 + x2) / 2 - page_w / 2) > _CENTERED_TOLERANCE * page_w:
                continue
            # 跨页重复的 running header 不救（真页眉每页出现）
            if text_counts.get(text, 0) > 1:
                continue
            # 救援：转为 title（level 2），与 paragraph_title 渲染层级一致
            block["type"] = BlockType.TITLE
            block["level"] = 2
            rescued.append(block)
        for block in rescued:
            discarded.remove(block)
            preproc.append(block)
        if rescued:
            # 保持 preproc_blocks 按 index 排序，保证标题排在表格之前
            preproc.sort(key=lambda b: b.get("index", 0))


# 发票右上角元数据字段标签（电子发票/专用发票顶栏右侧），
# VLM 常误判为 header 而丢弃。仅救援「标签:值」且值非空的字段。
_INVOICE_FIELD_LABELS = ("发票代码", "发票号码", "开票日期", "校验码", "机器编号")

# 票号前缀（专用发票「No 02269458」形式）
_NO_TICKET_RE = re.compile(r"^No\s+\d")


def _is_invoice_field_text(text: str) -> bool:
    """判断文本是否为发票右上角元数据字段（标签:值 形式）。

    必然正确条件：文本以发票字段标签开头，紧跟全角/半角冒号，且冒号后
    有非空值（如「发票代码：036002200111」）；或以「No 」+ 数字开头
    （票号）。其余文本（含孤立的标签、裸数字、监制章等）一律不救。

    Args:
        text: 待判断的文本（已 strip）。

    Returns:
        True 表示该文本为发票元数据字段。
    """
    t = text.strip()
    if not t:
        return False
    for label in _INVOICE_FIELD_LABELS:
        # 需要「标签 + 冒号 + 非空值」，如「发票代码：036002200111」；
        # 「机器编号：」这类孤立标签（无值）不救。
        if t.startswith(label) and len(t) > len(label) + 1 and t[len(label)] in ("：", ":"):
            if t[len(label) + 1:].strip():
                return True
    if _NO_TICKET_RE.match(t):
        return True
    return False


def rescue_discarded_invoice_fields(pdf_info_list: list) -> None:
    """将被误判为 header 的发票右上角元数据字段从 discarded_blocks 救回正文。

    VLM 对电子发票右上角的「发票代码/发票号码/开票日期/校验码」等字段
    分类不稳定，常判为 header 而被 hybrid 转换（hybrid_magic_model.py）
    归入 discarded_blocks 丢弃。本函数识别「标签:值」形式的发票元数据字段，
    改判为 text 并移回 preproc_blocks，恢复丢失的发票字段。

    Args:
        pdf_info_list: middle.json 的 pdf_info 列表（就地修改）。
    """
    for page_info in pdf_info_list:
        preproc = page_info.setdefault("preproc_blocks", [])
        discarded = page_info.get("discarded_blocks", [])
        rescued: list[dict] = []
        for block in discarded:
            if block.get("type") not in (BlockType.HEADER, BlockType.FOOTER):
                continue
            text = _block_text(block).strip()
            if not _is_invoice_field_text(text):
                continue
            # 救援：转为普通文本块
            block["type"] = BlockType.TEXT
            rescued.append(block)
        for block in rescued:
            discarded.remove(block)
            preproc.append(block)
        if rescued:
            # 保持 preproc_blocks 按 index 排序，保证字段位于表格之前的正确位置
            preproc.sort(key=lambda b: b.get("index", 0))
