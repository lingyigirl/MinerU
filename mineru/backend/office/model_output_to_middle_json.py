# Copyright (c) Opendatalab. All rights reserved.
import re
from collections import defaultdict

from mineru.backend.utils.html_image_utils import replace_inline_table_images, save_span_image_if_needed
from mineru.backend.office.office_magic_model import MagicModel
from mineru.utils.enum_class import BlockType
from mineru.version import __version__


def blocks_to_page_info(page_blocks, image_writer, page_index) -> dict:
    """将blocks转换为页面信息"""

    magic_model = MagicModel(page_blocks)
    image_blocks = magic_model.get_image_blocks()
    table_blocks = magic_model.get_table_blocks()
    chart_blocks = magic_model.get_chart_blocks()

    if image_writer:

        # Write embedded images to local storage via image_writer
        for img_block in image_blocks:
            for sub_block in img_block.get("blocks", []):
                if sub_block.get("type") != "image_body":
                    continue
                for line in sub_block.get("lines", []):
                    for span in line.get("spans", []):
                        save_span_image_if_needed(span, image_writer, page_index)

        replace_inline_table_images(table_blocks, image_writer, page_index)

        # Replace inline base64 images inside chart content with local paths
        for chart_block in chart_blocks:
            for sub_block in chart_block.get("blocks", []):
                if sub_block.get("type") != "chart_body":
                    continue
                for line in sub_block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") != "chart":
                            continue
                        save_span_image_if_needed(span, image_writer, page_index)

    title_blocks = magic_model.get_title_blocks()
    discarded_blocks = magic_model.get_discarded_blocks()
    list_blocks = magic_model.get_list_blocks()
    index_blocks = magic_model.get_index_blocks()
    text_blocks = magic_model.get_text_blocks()
    interline_equation_blocks = magic_model.get_interline_equation_blocks()

    page_blocks = []
    page_blocks.extend([
        *image_blocks,
        *chart_blocks,
        *table_blocks,
        *title_blocks,
        *text_blocks,
        *interline_equation_blocks,
        *list_blocks,
        *index_blocks,
    ])
    # 对page_blocks根据index的值进行排序
    page_blocks.sort(key=lambda x: x["index"])

    page_info = {"para_blocks": page_blocks, "discarded_blocks": discarded_blocks, "page_idx": page_index}
    return page_info


def _extract_section_parts_from_content(content: str, level: int):
    """Try to extract a leading section number (e.g. '1.2.1') from title content.

    Returns a list of ints [n1, n2, ..., nL] when the number of parts equals
    `level`, otherwise None.  Handles formats like:
        '1心肌特异性...'       (no separator)
        '1.2.1建立...'         (Chinese text immediately after number)
        '2.2.1 ALKBH5 ...'    (space separator)
    """
    match = re.match(r'^(\d+(?:\.\d+)*)', content.strip())
    if match:
        parts = [int(p) for p in match.group(1).split('.')]
        if len(parts) == level:
            return parts
    return None


def _collect_index_text_blocks(index_block: dict, result: list[dict]) -> None:
    """Depth-first collect TOC leaf text blocks."""
    for child in index_block.get("blocks", []):
        if child.get("type") == BlockType.INDEX:
            _collect_index_text_blocks(child, result)
        elif child.get("type") == BlockType.TEXT:
            result.append(child)


def _link_index_entries_by_anchor(middle_json: dict) -> None:
    """Keep TOC anchors only when they exist on parsed body blocks."""
    pdf_info = middle_json.get("pdf_info", [])
    valid_anchors: set[str] = set()

    for page_info in pdf_info:
        for block in page_info.get("para_blocks", []):
            anchor = block.get("anchor")
            if isinstance(anchor, str) and anchor.strip():
                valid_anchors.add(anchor.strip())

    if not valid_anchors:
        return

    for page_info in pdf_info:
        for block in page_info.get("para_blocks", []):
            if block.get("type") != BlockType.INDEX:
                continue
            toc_text_blocks: list[dict] = []
            _collect_index_text_blocks(block, toc_text_blocks)
            for text_block in toc_text_blocks:
                anchor = text_block.get("anchor")
                if not isinstance(anchor, str):
                    text_block.pop("anchor", None)
                    continue
                anchor = anchor.strip()
                if not anchor or anchor not in valid_anchors:
                    text_block.pop("anchor", None)
                    continue
                text_block["anchor"] = anchor


def result_to_middle_json(model_output_blocks_list, image_writer):
    middle_json = {"pdf_info": [], "_backend":"office", "_version_name": __version__}
    for index, page_blocks in enumerate(model_output_blocks_list):
        page_info = blocks_to_page_info(page_blocks, image_writer, index)
        middle_json["pdf_info"].append(page_info)

    section_counters: dict[int, int] = defaultdict(int)
    for page_info in middle_json["pdf_info"]:
        for block in page_info.get("para_blocks", []):
            if block.get("type") != BlockType.TITLE:
                continue
            level = block.get("level", 1)
            if block.get("is_numbered_style", False):
                # Ensure all ancestor levels start at 1 (never 0)
                for ancestor in range(1, level):
                    if section_counters[ancestor] == 0:
                        section_counters[ancestor] = 1
                # Increment current level counter and reset all deeper levels
                section_counters[level] += 1
                for deeper in list(section_counters.keys()):
                    if deeper > level:
                        section_counters[deeper] = 0
                # Build section number string, e.g. "1.2.1"
                section_number = ".".join(
                    str(section_counters[l]) for l in range(1, level + 1)
                )
                block["section_number"] = section_number
            else:
                # Some documents embed the section number directly in the content
                # (is_numbered_style=False).  Parse it and sync the counters so
                # that subsequent numbered blocks continue from the right base.
                lines = block.get("lines", [])
                content = ""
                if lines and lines[0].get("spans"):
                    content = lines[0]["spans"][0].get("content", "")
                parts = _extract_section_parts_from_content(content, level)
                if parts:
                    for k, v in enumerate(parts, start=1):
                        section_counters[k] = v
                    # Reset all deeper levels
                    for deeper in list(section_counters.keys()):
                        if deeper > level:
                            section_counters[deeper] = 0

    _link_index_entries_by_anchor(middle_json)
    return middle_json


def _normalize_to_vlm_format(middle_json: dict, page_width_pt: int = 595, page_height_pt: int = 842) -> dict:
    """Normalize DOCX/PPTX/XLSX middle_json to match VLM backend format.

    - ``_backend``: ``"office"`` -> ``"vlm"``
    - ``page_size``: added with ``[page_width_pt, page_height_pt]``
    - ``angle``: ``0`` for all blocks and spans
    - ``bbox``: placeholder ``[0, 0, 0, 0]`` for all spans
    - ``chart`` blocks -> ``image``
    - ``index`` blocks -> flattened ``list``
    - ``hyperlink`` spans -> ``text`` with URL merged into content
    - Nested ``list`` structures -> flattened
    """
    middle_json["_backend"] = "vlm"

    def _normalize_block(block: dict) -> None:
        block.setdefault("angle", 0)
        block.setdefault("lines", [])
        block_type = block.get("type", "")
        if block_type == "chart":
            block["type"] = "image"
        if block_type == "title" and block.get("section_number"):
            sn = block.pop("section_number", "")
            lines = block.get("lines", [])
            if lines and lines[0].get("spans"):
                lines[0]["spans"][0]["content"] = f"{sn} {lines[0]['spans'][0].get('content', '')}"
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                _normalize_span(span)
        for child in block.get("blocks", []):
            _normalize_block(child)

    def _normalize_span(span: dict) -> None:
        span.setdefault("angle", 0)
        span.setdefault("bbox", [0, 0, 0, 0])
        if span.get("type") == "hyperlink":
            span["type"] = "text"
            url = span.pop("url", "")
            content = span.get("content", "")
            span["content"] = f"[{content}]({url})" if content and url else (url or content)
        for child_span in span.get("spans", []):
            _normalize_span(child_span)

    def _flatten_docx_list(list_block: dict) -> None:
        flat_items = []
        for child in list_block.get("blocks", []):
            if child.get("type") == "list":
                _flatten_docx_list(child)
                flat_items.extend(child.get("blocks", []))
            else:
                _normalize_block(child)
                flat_items.append(child)
        list_block["blocks"] = flat_items

    def _flatten_index_block(index_block: dict) -> list[dict]:
        items = []
        for child in index_block.get("blocks", []):
            if child.get("type") == "index":
                items.extend(_flatten_index_block(child))
            elif child.get("type") == "text":
                _normalize_block(child)
                items.append(child)
        return items

    for page_info in middle_json.get("pdf_info", []):
        page_info["page_size"] = [page_width_pt, page_height_pt]
        new_blocks = []
        for block in page_info.get("para_blocks", []):
            block_type = block.get("type", "")
            if block_type == "index":
                list_items = _flatten_index_block(block)
                if list_items:
                    new_blocks.append({
                        "type": "list", "attribute": "unordered", "ilevel": 0,
                        "blocks": list_items, "angle": 0,
                        "index": block.get("index", 0),
                    })
            elif block_type == "list":
                _flatten_docx_list(block)
                _normalize_block(block)
                new_blocks.append(block)
            else:
                _normalize_block(block)
                new_blocks.append(block)
        page_info["para_blocks"] = new_blocks

    return middle_json
