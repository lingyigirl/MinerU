"""金额千分位逗号被读成点号的确定性回写。

VLM（MinerU2.5-Pro）在生成表格 HTML 时会偶发地把千分位分隔符 `,` 读成 `.`，
例如 `3,991,623.04` → `3.991.623.04`、`100,000.00` → `100.000.00`。该污染是
**VLM 原生**的：原始 model_output 的表格 content 内即已含点号形态，下游
（middle JSON 转换、OCR 单元格补充）逐 token 比对为零改动。实测新发银行流水
96 页中 26 个 token 落在 2 页（p19 25 处 / p84 1 处），且全部位于表格 `<td>`
内、表格之外 0 处。

**为什么可以确定性回写而非「猜」**
一个十进制字面量最多只有一个小数点。当 token 同时满足「≥2 个点」+「除末段外
每段恰好 3 位」+「末段恰好 2 位」时（`3.991.623.04`），它在任何进制下都不是
合法数字，每个非末位点**必然且只能**是千分位分隔符。该映射一一对应：不新增
数字、不丢弃数字、不改变量级，属解码而非推断，与「多点数形态按铁律不猜」
（不改写无证据的形态）原则不冲突——此处证据即「该形态不可能是数字」。

**安全闸门**（全部为廉价硬约束，任一不过即不改写）
  1. 整格匹配：只处理「整格文本 = 点号金额」的单元格，绝不在长串内部改写
     （拦 `NO.138.001.23A`、`凭证 3.991.623.04 号`）。
  2. 头段锚定 `\\d{1,3}`：4 位头段意味着本不需千分位，据此排除日期
     `2025.03.07`（`2025` 不匹配 `\\d{1,3}`）。
  3. 尾段恰为 2 位：排除无尾段的歧义形态 `1.234.567`（版本号/编号类）；
     实测该形态在本文档 0 处。
  4. 中段恰为 3 位：排除非法分组 `1.2345.67`。
  5. 表级金额上下文：表内必须存在金额证据（某列以「含逗号」或「纯小数」的
     金额形态为主，或表头含金额关键词）。无金额证据的表整表不介入，据此
     排除「非金额表里整列恰好都是点号串」这一类（如电话号列表）。
  6. 列级金额投票：表级通过后，仅该列已确立值以金额形态为主才介入。

闸门 5 / 6 的分工是必要的：二者都无法单独排除「整列统一为点号形态的非金额
列」——点号形态必须计入投票命中，否则 p19「余额」列（20/20 全点号、0 处逗号）
amount_ratio = 0 会被判非金额列，该列 20 处修复全部落空；而正因为点号计入命中，
一个整列点号的非金额列也无法被列投票区分。故先用表级闸门把「不存在任何金额
证据的表」整体排除（电话号列表所属的表通常无金额证据），再在表内做列投票。

**残留边界**：若某张表既有金额列、又有一列整列呈 `d{1,3}.d{3}.dd` 的非金额值，
本模块仍会改写后者。该形态本身即强证据（无合法数字读法），实测四份文档
（工行/威海/滕悦/新发）零误判；此处如实记录，不声称已完全排除。

本模块作为路由中立的唯一实现，供 hybrid 与 VLM 两个后端共同调用（缺陷源自
VLM，两条后端都会复现）。必须在 build_para_blocks_from_preproc 之前执行——
表 HTML 此后被复制进 para_blocks 与 content_list，再改不可逆。
"""

import re

from bs4 import BeautifulSoup
from loguru import logger

from mineru.utils.custom.table_utils._common import (
    _AMOUNT_PLAIN,
    _AMOUNT_RATIO_MIN,
    _AMOUNT_STRICT,
    _MIN_PROFILE_SAMPLES,
    _normalize_for_matching,
)

# 点号形态的千分位金额：与 _AMOUNT_STRICT 逐位对应
#   _AMOUNT_STRICT  ^\d{1,3}(,\d{3})*\.\d{2}$             ← 正确形态
#   _AMOUNT_DOTTED  ^[¥$−+-]?\d{1,3}(?:\.\d{3})+\.\d{2}$  ← 同一数字被读成点号
# 前导货币符号/正负号可选（归一化后 ￥→¥、－→-），匹配于 normalized 文本。
_AMOUNT_DOTTED = re.compile(r"^[¥$−+-]?\d{1,3}(?:\.\d{3})+\.\d{2}$")

# 表级金额上下文关键词（银行流水 + 发票/财务报表常见的金额口径）。
# 不复用 ocr_align._NUMERIC_COLUMN_KEYWORDS——该集合面向发票（数量/单价/金额/
# 税率/税额），不含「余额/发生额/借方/贷方」，对流水类表格会漏判。
_MONEY_HEADER_KEYWORDS = (
    "金额", "余额", "发生额", "借方", "贷方", "收入", "支出",
    "合计", "小计", "总计", "税额", "价税", "单价", "汇率",
)


def _transcode_dotted_amount(text: str) -> str | None:
    """点号形态金额 → 逗号形态（非末位分隔符改逗号，末位保留为小数点）。

    匹配在归一化文本上进行（_normalize_for_matching 只做全角→半角，
    因此 `３。９９１。６２３。０４` 亦能被识别）；返回值同样是归一化后的
    半角逗号形态，即每次改写都产出规范金额串。

    Args:
        text: 待判断的单元格文本。

    Returns:
        修复后的半角逗号形态金额；不匹配（含任何非金额字符）返回 None。
    """
    norm = _normalize_for_matching(text).strip()
    if not _AMOUNT_DOTTED.match(norm):
        return None
    seps = [i for i, ch in enumerate(norm) if ch == "."]
    if len(seps) < 2:
        return None  # 理论上不可达：正则已保证至少一个中段 + 一个小数点
    inner = set(seps[:-1])
    return "".join("," if i in inner else ch for i, ch in enumerate(norm))


def _amount_columns(data_rows: list[list[str]], include_dotted: bool) -> set[int]:
    """按列投票，返回「已确立值以金额形态为主」的列索引集合。

    判据取自「该列已有值长什么样」，与列类型解耦（表头关键词推断不可靠：
    新发流水 11 列实测全部被判为 text，number 列数为 0）。阈值与样本下限
    沿用守卫 12 的 _AMOUNT_RATIO_MIN / _MIN_PROFILE_SAMPLES，但不改动
    _build_column_profiles 本身，以免影响守卫 12 的既有语义。

    Args:
        data_rows: 表格数据行文本网格（不含表头行，可含空串）。
        include_dotted: 是否把点号形态计入金额命中。表级闸门用 False
            （只认「含逗号」/「纯小数」这种无歧义的金额形态），表内列投票
            用 True（点号列也须获授权，见模块 docstring 闸门 5/6 说明）。

    Returns:
        金额列索引集合；无满足条件的列时为空集。
    """
    col_count = max((len(row) for row in data_rows), default=0)
    amount_cols: set[int] = set()
    for vc in range(col_count):
        values = [
            _normalize_for_matching(row[vc])
            for row in data_rows
            if vc < len(row) and row[vc] and row[vc].strip()
        ]
        if len(values) < _MIN_PROFILE_SAMPLES:
            continue
        hits = 0
        for v in values:
            if _AMOUNT_STRICT.match(v) or _AMOUNT_PLAIN.match(v):
                hits += 1
            elif include_dotted and _AMOUNT_DOTTED.match(v):
                hits += 1
        if hits / len(values) >= _AMOUNT_RATIO_MIN:
            amount_cols.add(vc)
    return amount_cols


def _has_money_context(header_texts: list[str], data_rows: list[list[str]]) -> bool:
    """表级闸门：该表是否存在金额证据（无证据则整表不介入）。

    证据取其一即可：
      a. 表头含金额关键词（余额/发生额/借方/贷方/金额…）；
      b. 存在一列以「无歧义金额形态」（含逗号 或 纯小数）为主 —— 见
         _amount_columns(include_dotted=False)。

    Args:
        header_texts: 表头行全部单元格文本。
        data_rows: 表格数据行文本网格。

    Returns:
        True 表示该表有金额上下文，允许进入列级投票。
    """
    joined = _normalize_for_matching(" ".join(header_texts))
    if any(kw in joined for kw in _MONEY_HEADER_KEYWORDS):
        return True
    return bool(_amount_columns(data_rows, include_dotted=False))


def repair_dotted_amount_separators(pdf_info_list: list) -> int:
    """回写表格单元格中被读成点号的千分位分隔符（就地修改 span["html"]）。

    逐页遍历 preproc_blocks 中 type == 'table' 的 span，解析表格网格；
    先过表级金额上下文闸门，再以数据行按列投票确定金额列，仅对这些列内
    「整格文本匹配点号金额形态」的单元格改写为逗号形态。

    幂等：改写后单元格不再匹配 _AMOUNT_DOTTED，第二次调用零改写
    （列投票仍通过，但无可改写的格）。

    Args:
        pdf_info_list: 中间 JSON 的页面列表。

    Returns:
        修复的单元格数量（调试与回归测试用）。
    """
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    total_fixes = 0

    for page_idx, page_info in enumerate(pdf_info_list):
        for block in page_info.get("preproc_blocks", []):
            for span in iter_block_spans(block):
                if span.get("type") != ContentType.TABLE:
                    continue

                raw_html = span.get("html", "")
                if not raw_html:
                    continue

                soup = BeautifulSoup(raw_html, "html.parser")
                table = soup.find("table")
                if not table:
                    continue

                rows = [tr.find_all(["td", "th"]) for tr in table.find_all("tr")]
                if len(rows) < 2:
                    continue  # 仅表头行，无数据行可投票

                header_texts = [cell.get_text().strip() for cell in rows[0]]
                data_rows = [[cell.get_text().strip() for cell in row] for row in rows[1:]]

                if not _has_money_context(header_texts, data_rows):
                    continue
                amount_cols = _amount_columns(data_rows, include_dotted=True)
                if not amount_cols:
                    continue

                page_fixes = 0
                for row_idx, row in enumerate(rows):
                    if row_idx == 0:
                        continue  # 表头行不改写
                    for col_idx, cell in enumerate(row):
                        if col_idx not in amount_cols:
                            continue
                        if len(cell.contents) != 1:
                            continue  # 嵌套标签格：不扁平化，保守跳过
                        old_text = cell.get_text()
                        fixed = _transcode_dotted_amount(old_text)
                        if fixed is None or fixed == old_text:
                            continue
                        logger.info(
                            f"金额千分位回写: 页{page_idx} 列{col_idx} "
                            f"{old_text.strip()!r} -> {fixed!r}"
                        )
                        cell.string = fixed
                        page_fixes += 1

                if not page_fixes:
                    continue

                if span.get("html", "") != raw_html:
                    logger.warning(
                        f"金额千分位回写: 页{page_idx} span html 在读取后已被修改，跳过"
                    )
                    continue
                span["html"] = str(soup)
                total_fixes += page_fixes

    if total_fixes:
        logger.info(f"金额千分位回写完成: 共修复 {total_fixes} 个单元格")
    return total_fixes


__all__ = [
    "repair_dotted_amount_separators",
    "_transcode_dotted_amount",
    "_amount_columns",
    "_has_money_context",
]
