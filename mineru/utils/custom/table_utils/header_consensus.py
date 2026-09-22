"""跨页表头一致性归一化（守卫 11 扩展版）。

同一张跨页表格的表头应当逐页一致；被印章叠印污染时会出现「基准 + 印章词」
的前/后缀变体。本模块以「同列覆盖度最高的文本」为基准做双向（前缀/后缀）
剥离，并强制要求剥离部分落在文档印章词表内。

安全性设计：无印章证据时一律不改写——「单页截断」与「少量干净页 + 多数
污染页」两种形状在列内不可区分，缺少印章证据时任何剥离都是猜测，而猜错的
代价是损坏正确表头（违反「输出不多不少」原则）。

本模块从 ocr_supplement._normalize_header_text_across_pages 提取并扩展，
作为路由中立的唯一实现，供 hybrid 与 VLM 两个后端共同调用。
"""

import re

from bs4 import BeautifulSoup
from loguru import logger

# CJK-only 字符范围（统一表意文字）
_CJK_CHAR = re.compile(r"^[一-鿿]+$")

# 污染形式允许的最大长度差（印章叠印文字 2-4 个汉字）
_MAX_AFFIX_LEN = 4


def _is_cjk_only(text: str) -> bool:
    """检查字符串是否仅含 CJK 字符（排除数字、标点、字母）。"""
    return bool(_CJK_CHAR.match(text))


def _affix_form(text: str, base: str) -> tuple[str | None, bool]:
    """判断 text 是否为 base 的脏形式，返回 (剥离部分, 是否前缀污染)。

    覆盖两种方向：
      - 后缀污染（原生守卫 11 场景）："转出金额专用章" -> 剥离 "专用章"
      - 前缀污染（滕悦场景）：       "专用章转出金额" -> 剥离 "专用章"
    长度差须落在 (0, _MAX_AFFIX_LEN] 内，否则视为无关文本。

    Args:
        text: 待判断的表头文本。
        base: 该列共识基线文本。

    Returns:
        (None, False) 表示不是脏形式；否则 (stripped, is_prefix)。
    """
    length_diff = len(text) - len(base)
    if length_diff <= 0 or length_diff > _MAX_AFFIX_LEN:
        return None, False
    if text.startswith(base):
        return text[len(base):], False
    if text.endswith(base):
        return text[:len(text) - len(base)], True
    return None, False


def _pick_column_base(texts: list[str]) -> str | None:
    """从同列全部表头文本中选出共识基线。

    选「能解释最多同列变体」者：该候选本体 + 其前/后缀脏形式覆盖的条目数
    最多；覆盖数相同取较短文本（按 (长度, 字典序) 升序遍历，仅严格大于时
    替换，结果确定）。覆盖数 < 2 视为共识不足，整列跳过。

    为什么不用「最短文本」或「众数」做基线：
      - 最短文本：同列索引可能被文档内另一张列结构不同的表格污染（滕悦
        6 页另一格式流水表把「余额」放在第 9 列），最短文本会选中「余额」，
        真正的「转出金额」永远选不中，守卫整体空转；
      - 众数：印章叠印会污染**多数**页面（滕悦 32/42 页被污染），
        众数恰恰是被污染的形式。
    覆盖度同时化解这两个陷阱（滕悦实测：转出金额覆盖 42，余额仅 6）。

    注意：覆盖度对「单页截断」形状（如 10 页「转出金额」+ 1 页「转出」）
    会把基线选成截断值「转出」——该形状与「少量干净页 + 多数印章污染页」
    在结构上不可区分（都是"多数长、少数短"）。因此安全性不靠基线选择，
    而靠改写前的印章词表闸门（见 normalize_table_headers_across_pages
    校验 2）：无印章证据则一律不改写。

    Args:
        texts: 该列全部表头文本（含跨页重复）。

    Returns:
        基线文本；共识不足返回 None。
    """
    best_base = ""
    best_cover = 0
    for cand in sorted(set(texts), key=lambda t: (len(t), t)):
        cover = 0
        for t in texts:
            if t == cand or _affix_form(t, cand)[0] is not None:
                cover += 1
        if cover > best_cover:
            best_base, best_cover = cand, cover
    if best_cover < 2 or len(best_base) < 2:
        return None
    return best_base


def _collect_doc_seals(pdf_info_list: list) -> set:
    """收集文档级印章文本（跨页超集），用于剥离合法性校验。

    复用 ocr_supplement 的实现（守卫 5 已在使用同一份语义）。此处延迟导入：
    ocr_supplement 为保持 `_normalize_header_text_across_pages` 兼容入口而在
    函数体内反向引用本模块，模块级互引会成环。
    """
    from mineru.utils.custom.table_utils.ocr_supplement import _collect_doc_seals as _fn
    return _fn(pdf_info_list)


def normalize_table_headers_across_pages(pdf_info_list: list) -> int:
    """跨页对齐表头文字：前置/后置污染双向检测与修复（守卫 11 扩展版）。

    规则（原则 9 跨页对照）：
    1. 逐列收集所有页面的表头文本
    2. 基准 = 该列覆盖度最高（本体 + 前/后缀脏形式覆盖条目最多）的文本，
       覆盖数相同取较短者，覆盖数 < 2 的列跳过（见 _pick_column_base）
    3. 对每个 != 基准的文本：
       a. 若 text 以 base 开头且长度差 1..4 → 后缀污染
       b. 若 text 以 base 结尾且长度差 1..4 → 前缀污染
       c. 剥离部分必须 CJK-only 且 >= 2 字符
       d. 剥离部分必须是某印章文本的子串（无印章证据则跳过）
    4. 每处替换以 INFO 级别记录前后对比

    幂等：已归一的列在第二次调用时覆盖数/剥离条件不再成立，零改写。

    Args:
        pdf_info_list: 中间 JSON 的页面列表（就地修改 span["html"]）。

    Returns:
        修正的表头格数量（调试与回归测试用）。
    """
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    # 收集印章文本用于剥离合法性校验
    doc_seals = _collect_doc_seals(pdf_info_list)

    # col_idx -> [(page_idx, table_idx, text, cell, soup, span, raw_html)]
    entries: dict[int, list] = {}

    for page_idx, page_info in enumerate(pdf_info_list):
        table_idx = 0
        for block in page_info.get("preproc_blocks", []):
            for span in iter_block_spans(block):
                if span.get("type") != ContentType.TABLE:
                    continue
                html = span.get("html", "")
                if not html:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                table = soup.find("table")
                if not table:
                    continue
                first_row = table.find("tr")
                if not first_row:
                    continue
                for col_idx, cell in enumerate(first_row.find_all(["td", "th"])):
                    text = cell.get_text().strip()
                    if text and len(text) >= 2:
                        entries.setdefault(col_idx, []).append(
                            (page_idx, table_idx, text, cell, soup, span, html)
                        )
                table_idx += 1

    total_fixes = 0
    for col_idx, col_entries in entries.items():
        if len(col_entries) < 2:
            continue

        texts = [e[2] for e in col_entries]
        base = _pick_column_base(texts)
        if base is None:
            continue

        col_fixes = 0
        for page_idx, _table_idx, text, cell, soup, span, raw_html in col_entries:
            if text == base:
                continue

            stripped, is_prefix = _affix_form(text, base)
            if stripped is None:
                continue  # 不是基准的单纯前/后缀形式（或后缀过长）

            # 校验 1：剥离部分必须 CJK-only、>= 2 字符、无数字标点
            if not _is_cjk_only(stripped) or len(stripped) < 2:
                continue

            # 校验 2（强闸门）：必须有印章证据，且剥离部分须为某印章文本
            # 的子串。这是安全性所在——「单页截断」与「少量干净页 + 多数
            # 印章污染页」在列内结构上不可区分（都是"多数长、少数短"），
            # 若无印章证据仅靠字长差剥离，会把合法表头（"对方单位" 与
            # "对方单位名称" 分属两张表）误伤。因此无印章证据一律不改写。
            if not doc_seals:
                logger.warning(
                    f"表头一致性归一化: 列{col_idx} 页{page_idx} {text!r} 的"
                    f"可疑剥离 {stripped!r} 无印章证据，保守跳过"
                )
                continue
            if not any(stripped in seal_text for seal_text in doc_seals):
                logger.warning(
                    f"表头一致性归一化: 列{col_idx} 页{page_idx} {text!r} 的"
                    f"可疑剥离 {stripped!r} 不在印章词表内，保守跳过"
                )
                continue

            # 替换表头文本
            for child in list(cell.children):
                child.extract()
            cell.string = base
            new_html = str(soup)
            if span.get("html", "") == raw_html:
                span["html"] = new_html
            else:
                logger.warning(
                    f"表头一致性守卫: span html 在读取后已被修改（列{col_idx}），跳过"
                )

            direction = "前缀" if is_prefix else "后缀"
            logger.info(
                f"表头一致性归一化: 列{col_idx} 页{page_idx} "
                f"{direction}污染 {text!r} -> {base!r} "
                f"(剥离 {stripped!r}, 长度差 {len(text) - len(base)})"
            )
            col_fixes += 1

        if col_fixes:
            total_fixes += col_fixes

    if total_fixes:
        logger.info(f"表头一致性归一化完成: 共修正 {total_fixes} 个表头格")
    return total_fixes


__all__ = [
    "normalize_table_headers_across_pages",
    "_affix_form",
    "_is_cjk_only",
    "_pick_column_base",
]
