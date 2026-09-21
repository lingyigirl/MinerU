"""共享基础工具与关键词常量。

本包内所有子模块的公共底座：不依赖任何其他子模块，只提供
纯函数级的文本/数值/单元格判定工具与跨模块共用的关键词集合。"""

import re
import unicodedata
from typing import Optional
from bs4 import Tag


# 常见发票表头关键词集合（用于识别合并单元格文本中的表头部分）
_INVOICE_HEADER_KEYWORDS = {
    "项目名称", "货物或应税劳务、服务名称", "规格型号", "单位", "数量",
    "单价", "金额", "税率", "税额", "税率/征收率",
    "价税合计", "合计", "备注", "购买方", "销售方",
    "名称", "纳税人识别号", "地址", "电话", "开户银行",
    "银行账号", "收款人", "复核人", "开票人",
}


# 合计/小计类摘要关键词（用于识别被混入数据行中的合计标签）
_SPLIT_SUMMARY_KEYWORDS = {"合计", "小计", "总计", "本页小计", "累计"}


def _is_data_value(token: str) -> bool:
    """判断 token 是否为数据值（数字、百分比等）。

    Args:
        token: 待检查的 token。

    Returns:
        是否为数据值。
    """
    token = token.strip()
    # 含括号数值（如 "37577.97(度)"）
    if re.match(r'^[\d.]+\([^)]*\)$', token):
        return True
    # 货币金额
    if re.match(r'^[¥￥][\d.,]+$', token):
        return True
    # 百分比
    if re.match(r'^[\d.]+\s*%$', token):
        return True
    # 数字
    if re.match(r'^[\d.,]+$', token):
        return True
    # 中文表头后缀接数值（如 "金额222875.57"、"税额28973.82"、"数量319620"）
    # VLM 常将表头标签和数值拼接在同一个 token 中，此处提取尾部数值部分
    if re.match(r'^[一-鿿]+[¥￥]?\d[\d.,]*$', token):
        return True
    return False


def _is_text_token(token: str) -> bool:
    """判断 token 是否为文本类型（而非数值类型）。

    Args:
        token: 待判断的 token。

    Returns:
        True 表示文本类型。
    """
    return not _is_data_value(token)


def _get_cell_text(cells: list[Tag], idx: int) -> str:
    """安全获取单元格文本（处理索引越界）。

    Args:
        cells: 单元格列表。
        idx: 列索引。

    Returns:
        单元格文本，越界返回空字符串。
    """
    if 0 <= idx < len(cells):
        return cells[idx].get_text()
    return ""


def _compute_total_columns(rows: list[Tag]) -> int:
    """计算表格总列数（考虑 colspan 的最大扩展列数）。

    Args:
        rows: BeautifulSoup <tr> Tag 列表。

    Returns:
        总列数（整数，最小为 1）。
    """
    max_cols = 1
    for row in rows:
        col_count = 0
        for cell in row.find_all(["td", "th"]):
            colspan = int(cell.get("colspan", 1))
            col_count += colspan
        if col_count > max_cols:
            max_cols = col_count
    return max_cols


def _strip_header_prefix(token: str) -> Optional[dict]:
    """检查 token 是否以已知表头关键词开头，若匹配则剥离。

    VLM 对纵向排版的表头（如「数量」「单价」上下两字）会输出为「数 量」、
    「单 价」（关键词内部含空格），此时直接前缀匹配会失败。因此先按原文本
    直接前缀匹配（保留数据值内部空格），失败时再用去除全部空白后的紧凑文本
    匹配，容忍关键词内部空格（与 _match_data_column_keyword 保持一致）。

    Args:
        token: 待检查的 token。

    Returns:
        {"header": 表头关键词, "data": 剩余文本} 或 None。
    """
    # 紧凑文本：去除全部空白，用于容忍关键词内部空格（如「数 量」→「数量」）
    compact = "".join(token.split())
    for header in sorted(_INVOICE_HEADER_KEYWORDS, key=len, reverse=True):
        # 直接前缀匹配（保留数据值内部空格）
        if token.startswith(header) and len(token) > len(header):
            data = token[len(header):].strip()
            if data:
                return {"header": header, "data": data}
        # 关键词内部含空格（如「数 量1622」→「数量」+「1622」），
        # 仅在紧凑文本与原文不同且紧凑文本能前缀匹配时才用紧凑文本
        if compact != token and compact.startswith(header) and len(compact) > len(header):
            data = compact[len(header):].strip()
            if data:
                return {"header": header, "data": data}
    return None


def _strip_leading_punctuation(token: str) -> str:
    """去除 token 开头的非中文标点符号。

    场景：VLM 输出 ".规格型号" 中 "." 是表格竖线的误识别残留。

    Args:
        token: 原始 token。

    Returns:
        去除开头标点后的 token。
    """
    # 仅去除表格竖线误识别的残留符号（单个 "."），保留有意义的标点
    cleaned = re.sub(r'^\.(?=[^.\d])', '', token).strip()
    return cleaned or token


def _normalize_for_matching(text: str) -> str:
    """规范化文本用于模糊匹配。

    将 OCR 输出中常见的全角标点转换为半角，
    解决 VLM（半角）与 OCR（全角）之间的字符编码差异。

    Args:
        text: 待规范化的文本。

    Returns:
        规范化后的文本。
    """
    full_to_half = {
        "（": "(", "）": ")", "：": ":", "，": ",",
        "。": ".", "！": "!", "？": "?", "；": ";",
        "“": '"', "”": '"', "【": "[", "】": "]",
        "《": "<", "》": ">", "％": "%", "＋": "+",
        "－": "-", "＝": "=", "０": "0", "１": "1",
        "２": "2", "３": "3", "４": "4", "５": "5",
        "６": "6", "７": "7", "８": "8", "９": "9",
        " ": " ", "　": " ",
        "￥": "¥",   # OCR 全角人民币符号 → VLM 半角
    }
    result = text
    for full, half in full_to_half.items():
        result = result.replace(full, half)
    return result


def _classify_ocr_item_type(text: str) -> str:
    """判断 OCR 识别文字的语义类型。

    Args:
        text: OCR 识别的文字。

    Returns:
        'text' | 'number' | 'rate'。
    """
    text = text.strip()
    # 百分比
    if re.match(r'^[\d.]+\s*%$', text):
        return "rate"
    # 纯数字或货币
    if _is_data_value(text):
        return "number"
    return "text"


def _digits_only(text: str) -> str:
    """提取字符串中的数字字符，用于数值 token 的同行归一化比对（守卫 8）。

    去除非数字字符后比对，可消去不同语言区域的标点/分隔符差异
    （如 '3.649.36' vs '3,649.36' → digits '364936' == '364936'）。

    Args:
        text: 待提取的文本。

    Returns:
        仅含数字字符的字符串（无数字时返回空串）。
    """
    return re.sub(r"\D", "", text)


def _is_same_row_value_variant(ot: str, vlm_row: list[str]) -> bool:
    """判断数值 token 是否为同行已有值的 OCR 变体（守卫 8）。

    同行已有值的变体不是新信息，不得写入空列（原则 1 输出不多不少）。
    覆盖三种匹配模式：
      ① 数字归一化相等：3.649.36 ≡ 3,649.36（标点/分隔符差异）
      ② 截断残片：,300.75 ⊂ 8,300.75、10,000,000 ⊂ 10,000,000.00（长度差 ≤2）
      ③ 漏位/形近：1511101040027417 ← 15511101040027417（编辑距离 ≤1, min 长度 ≥6 位）

    Args:
        ot: OCR 识别 token 文本。
        vlm_row: VLM 同行所有非空单元格文本列表。

    Returns:
        是否为同行已有值的 OCR 变体。
    """
    d_ot = _digits_only(ot)
    if not d_ot:
        return any(ot == vt for vt in vlm_row if vt)
    for vt in vlm_row:
        if not vt:
            continue
        d_vt = _digits_only(vt)
        if not d_vt:
            continue
        # ① 数字归一化相等
        if d_ot == d_vt:
            return True
        # ② 截断残片（短的一侧 ≥5 位，长度差 ≤3；≤2 漏掉千位分组截断 e.g. 1,000,000.00→1,000,00 diff=3）
        shorter, longer = (d_ot, d_vt) if len(d_ot) <= len(d_vt) else (d_vt, d_ot)
        if (
            len(shorter) >= 5
            and len(longer) - len(shorter) <= 3
            and (longer.startswith(shorter) or longer.endswith(shorter))
        ):
            return True
        # ③ 漏位/形近（两侧均 ≥6 位，编辑距离 ≤1）
        if len(d_ot) >= 6 and len(d_vt) >= 6 and _edit_distance_le1(d_ot, d_vt):
            return True
    return False


def _is_pure_punctuation(text: str) -> bool:
    """判断文本是否为无信息量的短标点噪声（如「。」「、」「-」「..」）。

    用于在 OCR 文本过滤阶段排除纯标点噪声。判断两重：
    1. 全为标点（P）/符号（S）/空白（Z）字符；
    2. 长度 ≤ 2——「***」这类多字符纯符号串是银行流水/发票中的
       账号打码掩码（真实内容），不能丢弃（输出不少），仅丢弃短噪声。

    Args:
        text: 待检查的原始 OCR 文本。

    Returns:
        是否是无信息量的短标点噪声。
    """
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) > 2:
        return False
    return all(
        unicodedata.category(ch).startswith(("P", "Z", "S"))
        for ch in stripped
    )


def _is_single_cjk_char(text: str) -> bool:
    """判断规范化后的文本是否为单一个中文字符。

    Args:
        text: 规范化后的文本。

    Returns:
        是否恰为单个 CJK 字符。
    """
    return len(text) == 1 and "一" <= text <= "鿿"


def _is_merged_noise(text: str) -> bool:
    """检测 OCR 横向合并相邻单元格产生的拼接噪音。

    OCR 把相邻格读成单框且丢失分隔符，产生无合法语义的拼接串：
    模式 M1: MM-DD 紧接非数字内容（"01-06收"、"01-02对公收费"）
             —— 日期 `MM-DD` 后应只有空白/行尾，紧接其它字符说明
             跨了相邻列；`D`（非数字）守卫保证 `12-3456789` 这类
             账号不误伤。
    模式 M2: YYYY-MM-DD 紧接 HH:MM 无空格（"2025-02-1416:53:28"）
             —— 源 PDF 中交易日期与时间之间必有空格，无空格拼接
             即 OCR 丢失分隔符（合法时间串 "2025-02-14 16:53:28"
             中间是空格，不匹配）。

    Args:
        text: 待检查的原始 OCR 文本。

    Returns:
        是否为拼接噪音。
    """
    return bool(
        re.match(r"^\d{2}-\d{2}\D", text)
        or re.match(r"^\d{4}-\d{2}-\d{2}\d{2}:\d{2}", text),
    )


def _compact_norm(text: str) -> str:
    """去空白与分隔标点后的紧凑文本（印章/标题/截断比对用）。

    Args:
        text: 原始文本。

    Returns:
        去除空白与 `，。、,.:：（）()%％—-` 后的字符串。
    """
    return _SEAL_NOISE_CHARS.sub("", text or "")


def _cjk_count(text: str) -> int:
    """统计文本中的 CJK 统一表意文字个数。"""
    return len(_CJK_RE.findall(text or ""))


def _edit_distance_le1(a: str, b: str) -> bool:
    """判断两个规范化字符串的编辑距离是否 ≤1（早期退出）。

    用于 OCR 印章误识近匹配（如「枣庄三八支行」→「本庄三八支行」：
    枣→本 单字符替换）。长度差 >1 即不可能是 1 次编辑，直接返回。

    Args:
        a: 规范化后的字符串。
        b: 规范化后的字符串。

    Returns:
        编辑距离 ≤1 时为 True。
    """
    if abs(len(a) - len(b)) > 1:
        return False
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(m):
        prev = dp
        dp = [i + 1] + [0] * n
        for j in range(n):
            if a[i] == b[j]:
                cost = 0
            else:
                cost = 1
            dp[j + 1] = min(
                prev[j + 1] + 1,  # 删除 a[i]
                dp[j] + 1,  # 插入 b[j]
                prev[j] + cost,  # 替换
            )
        if min(dp) > 1:
            return False
    return dp[n] <= 1


# 守卫 5 用：语义源（页面 chrome）文本正则
_CJK_RE = re.compile(r"[一-鿿]")


_SEAL_NOISE_CHARS = re.compile(r"[\s，。、,.:：（）()%％—-]")


__all__ = [
    '_CJK_RE',
    '_INVOICE_HEADER_KEYWORDS',
    '_SEAL_NOISE_CHARS',
    '_SPLIT_SUMMARY_KEYWORDS',
    '_cjk_count',
    '_classify_ocr_item_type',
    '_compact_norm',
    '_compute_total_columns',
    '_digits_only',
    '_edit_distance_le1',
    '_get_cell_text',
    '_is_data_value',
    '_is_merged_noise',
    '_is_pure_punctuation',
    '_is_same_row_value_variant',
    '_is_single_cjk_char',
    '_is_text_token',
    '_normalize_for_matching',
    '_strip_header_prefix',
    '_strip_leading_punctuation',
]
