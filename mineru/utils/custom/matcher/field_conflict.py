"""字段级冲突解决：同一字段多源不同值时选择最优。"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_SOURCE_PRIORITY = ["kvp", "mineru_native", "vlm_direct"]


@dataclass
class FieldEntry:
    """统一字段条目。"""
    key: str
    value: str
    source: str                    # "mineru_native" | "kvp" | "vlm_direct"
    bbox: list[float]              # [x0, y0, x1, y1]
    confidence: float              # 源引擎置信度 [0, 1]
    page_idx: int = 0


def _adjust_confidence(field: FieldEntry) -> float:
    """按源引擎调整置信度（设计文档 §6.3）。"""
    if field.source == "kvp":
        return field.confidence + 0.15
    if field.source == "vlm_direct":
        return field.confidence - 0.1
    return field.confidence


def _group_by_key(fields: list[FieldEntry]) -> dict[str, list[FieldEntry]]:
    grouped: dict[str, list[FieldEntry]] = {}
    for f in fields:
        grouped.setdefault(f.key, []).append(f)
    return grouped


def resolve_field_conflicts(
    fields: list[FieldEntry],
    strategy: str = "confidence_voting",
    source_priority: list[str] | None = None,
) -> list[FieldEntry]:
    """字段级冲突解决。

    策略选项：
    - "confidence_voting": 选置信度最高者（默认，含源引擎置信度调整）
    - "source_priority": 按预设优先级 kvp > mineru_native > vlm_direct
    - "majority_voting": 多源一致的值优先，不一致取多数

    Args:
        fields: 待解决冲突的字段列表。
        strategy: 冲突解决策略标识。
        source_priority: source_priority 策略的优先级列表。

    Returns:
        去冲突后的字段列表（每 key 只保留一个）。
    """
    if not fields:
        return []

    grouped = _group_by_key(fields)
    result: list[FieldEntry] = []

    if strategy == "source_priority":
        prio = source_priority or _DEFAULT_SOURCE_PRIORITY
        rank = {s: i for i, s in enumerate(prio)}
        for key, fs in grouped.items():
            fs.sort(key=lambda f: (rank.get(f.source, len(prio)), -f.confidence))
            result.append(fs[0])
    elif strategy == "majority_voting":
        for key, fs in grouped.items():
            votes: dict[str, list[FieldEntry]] = {}
            for f in fs:
                votes.setdefault(f.value, []).append(f)
            best_value = max(votes.items(), key=lambda kv: len(kv[1]))[0]
            best = max(votes[best_value], key=_adjust_confidence)
            result.append(best)
    else:  # confidence_voting（默认）
        for key, fs in grouped.items():
            result.append(max(fs, key=_adjust_confidence))

    return result
