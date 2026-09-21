"""span 字符游程救援（gap-aware char rescue）。

**要解决的问题**

`span_pre_proc.fill_char_in_spans` 的归属判定要求字符中心点落在 span 框内
（`calculate_char_in_span`）。当 span 框被页面元素截断时，框外字符无人认领、
直接丢弃，表现为页眉字段残缺：

    span bbox [541, 44, 628, 52] → content '本方账号开户行：枣庄薛城'   缺「支行」
    span bbox [689, 27, 707, 38] → content '：元'                      缺「单位」

框的右/左边界恰好是印章 image 块的边缘（628.1 / 693.8），被切掉的字符落在
印章 x 区间内。这不是 OCR 看不清，而是这些字符**从未进入任何 span**。

**做法**

第一遍归属结束后，对「未被任何 span 认领」的字符做第二遍：
只吸收与 span 现有字符游程紧邻（水平间隙 ≤ ratio × span 高度）的同字号字符，
吸收后游程随之向外扩张，因此可以连续吸收「支」「行」两个字，并在
「支行」与「时间」之间的字段间距处自动停止。

**不做的事**

- 不改 span bbox：bbox 仍反映模型框，只有 `chars` 被补充。
- 不动已有归属：只处理第一遍没人认领的字符，已有 span 的内容不会被改写。
"""

from loguru import logger

from mineru.utils.custom.config import (
    get_span_gap_rescue_enable,
    get_span_gap_rescue_ratio,
)

# 字符高度 / span 高度 的允许比值区间：同字号字符应与之接近，
# 用于把表格行、相邻行的字符挡在页眉 span 之外。
_MIN_HEIGHT_RATIO = 0.5
_MAX_HEIGHT_RATIO = 1.5

# 单页最多迭代轮数。每轮至少吸收一个字符才会继续，正常情况 1~3 轮收敛。
_MAX_ROUNDS = 64

# 不参与救援的字符：没有墨迹宽度，坐标不可信；
# chars_to_content 会依据间隙自行补空格，无需在此纳入。
_SKIP_CHARS = frozenset(" \t\r\n ")


def _run_extent(span: dict) -> tuple[float, float] | None:
    """span 已归属字符的墨迹水平范围 (left, right)；无字符时返回 None。"""
    chars = span.get("chars") or []
    if not chars:
        return None
    return (
        min(c["bbox"][0] for c in chars),
        max(c["bbox"][2] for c in chars),
    )


def _is_same_line(char_bbox: list[float], span_bbox: list[float]) -> bool:
    """字符是否与 span 同一行：中心 y 在 span 纵向范围内，且字高与 span 高接近。"""
    span_height = span_bbox[3] - span_bbox[1]
    char_height = char_bbox[3] - char_bbox[1]
    if span_height <= 0 or char_height <= 0:
        return False
    if not (span_bbox[1] < (char_bbox[1] + char_bbox[3]) / 2 < span_bbox[3]):
        return False
    return _MIN_HEIGHT_RATIO <= char_height / span_height <= _MAX_HEIGHT_RATIO


def _gap_to_run(char_bbox: list[float], run: tuple[float, float], span_height: float, ratio: float) -> float | None:
    """字符到 span 现有游程的最小水平间隙，超出阈值或与游程重叠时返回 None。"""
    run_left, run_right = run
    gaps = [g for g in (run_left - char_bbox[2], char_bbox[0] - run_right) if g >= 0]
    if not gaps:
        return None
    gap = min(gaps)
    return gap if gap <= span_height * ratio else None


def rescue_isolated_chars(spans: list[dict], all_chars: list[dict]) -> int:
    """把紧邻 span 游程的未归属字符并入 span，返回吸收的字符数。

    Args:
        spans: 已由 fill_char_in_spans 第一遍归属过的 span 列表（每项含 bbox / chars）。
        all_chars: 本页原生文本层字符列表（每项含 char / bbox / char_idx）。
    """
    if not spans or not all_chars:
        return 0
    if not get_span_gap_rescue_enable():
        return 0

    ratio = get_span_gap_rescue_ratio()
    claimed = {id(c) for span in spans for c in (span.get("chars") or [])}
    unclaimed = [
        c
        for c in all_chars
        if id(c) not in claimed and c.get("char") not in _SKIP_CHARS
    ]
    if not unclaimed:
        return 0

    rescued = 0
    rescued_samples = []

    for _ in range(_MAX_ROUNDS):
        runs = {}
        for span in spans:
            run = _run_extent(span)
            if run is not None:
                runs[id(span)] = run

        # 本轮所有可行的 (间隙, 字符, span) 组合，按间隙升序 —— 先吸收最近的，
        # 使游程逐字向外生长，而不是一次跨过间隙较大的字。
        candidates = []
        for char in unclaimed:
            if id(char) in claimed:
                continue
            char_bbox = char["bbox"]
            for span in spans:
                run = runs.get(id(span))
                if run is None or not _is_same_line(char_bbox, span["bbox"]):
                    continue
                gap = _gap_to_run(char_bbox, run, span["bbox"][3] - span["bbox"][1], ratio)
                if gap is not None:
                    candidates.append((gap, char, span))

        if not candidates:
            break
        candidates.sort(key=lambda item: item[0])

        progressed = False
        for _gap, char, span in candidates:
            if id(char) in claimed:
                continue
            # 前面已吸收的字符会改变游程，这里用最新游程复核一次，
            # 不满足条件的留到下一轮重新评估。
            current_run = _run_extent(span)
            if current_run is None:
                continue
            gap = _gap_to_run(
                char["bbox"], current_run, span["bbox"][3] - span["bbox"][1], ratio
            )
            if gap is None:
                continue
            span["chars"].append(char)
            claimed.add(id(char))
            rescued += 1
            progressed = True
            if len(rescued_samples) < 5:
                rescued_samples.append(char["char"])

        if not progressed:
            break

    if rescued:
        logger.info(
            f"span游程救援——吸收 {rescued} 个字符（间隙≤{ratio}×span高度），"
            f"示例: {''.join(rescued_samples)}"
        )

    return rescued
