"""content_list_v2 输出后处理工具。

在上游 make_blocks_to_content_list_v2() 生成 content_list_v2 结构后，
对 LIST 类型段落中的每个 list_item 补充独立的 bbox 信息。

上游 merge 时此模块仅需保留，无需修改。
"""

from typing import Any


def enrich_list_items_with_bbox(
    para_content: dict[str, Any],
    para_block: dict[str, Any],
    page_size: tuple[int, int],
) -> None:
    """为 content_list_v2 中 LIST 类型的每个 list_item 补充独立 bbox。

    上游 make_blocks_to_content_list_v2() 生成的 LIST 段落中，
    list_items 只有 item_type 和 item_content，各条目共享父级 bbox。
    此函数从原始 para_block 中提取每个 block 的 bbox 并注入到对应的 list_item 中，
    坐标归一化公式与上游一致（×1000 / page_size）。

    Args:
        para_content: make_blocks_to_content_list_v2() 生成的段落内容，
                      会被原地修改（in-place）。
        para_block:   原始段落数据，包含 blocks 列表。
        page_size:    页面尺寸 (width, height)，用于 bbox 归一化。
    """
    if para_content.get("type") != "list":
        return

    list_items = para_content.get("content", {}).get("list_items")
    raw_blocks = para_block.get("blocks")
    if not list_items or not raw_blocks:
        return

    page_width, page_height = page_size
    if not page_width or not page_height:
        return

    for idx, raw_block in enumerate(raw_blocks):
        if idx >= len(list_items):
            break
        item_bbox = raw_block.get("bbox")
        if not item_bbox:
            continue
        ix0, iy0, ix1, iy1 = item_bbox
        list_items[idx]["bbox"] = [
            int(ix0 * 1000 / page_width),
            int(iy0 * 1000 / page_height),
            int(ix1 * 1000 / page_width),
            int(iy1 * 1000 / page_height),
        ]
