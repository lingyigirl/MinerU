# Copyright (c) Opendatalab. All rights reserved.
import os
import time

import numpy as np
from loguru import logger
from tqdm import tqdm

from mineru.backend.utils.html_image_utils import replace_inline_table_images
from mineru.backend.utils.para_block_utils import (
    build_para_blocks_from_preproc,
    cleanup_internal_para_block_metadata,
    merge_para_text_blocks,
)
from mineru.backend.utils.runtime_utils import cross_page_table_merge
from mineru.backend.vlm.vlm_magic_model import MagicModel
from mineru.utils.config_reader import get_table_enable, get_llm_aided_config
from mineru.utils.cut_image import cut_image_and_table
from mineru.utils.enum_class import ContentType
from mineru.utils.hash_utils import bytes_md5
from mineru.utils.pdfium_guard import close_pdfium_document, pdfium_guard
from mineru.version import __version__


heading_level_import_success = False
llm_aided_config = get_llm_aided_config()
if llm_aided_config:
    title_aided_config = llm_aided_config.get('title_aided', {})
    if title_aided_config.get('enable', False):
        from mineru.utils.llm_aided import llm_aided_title
        from mineru.backend.utils.ocr_det_utils import (
            detect_ocr_boxes_from_padded_crop,
            get_ch_lite_ocr_det_model,
        )
        heading_level_import_success = True


def blocks_to_page_info(page_blocks, image_dict, page, image_writer, page_index) -> dict:
    """将blocks转换为页面信息"""

    scale = image_dict["scale"]
    page_pil_img = image_dict["img_pil"]
    page_img_md5 = bytes_md5(page_pil_img.tobytes())
    with pdfium_guard():
        width, height = map(int, page.get_size())

    magic_model = MagicModel(page_blocks, width, height)
    image_blocks = magic_model.get_image_blocks()
    table_blocks = magic_model.get_table_blocks()
    chart_blocks = magic_model.get_chart_blocks()
    title_blocks = magic_model.get_title_blocks()
    discarded_blocks = magic_model.get_discarded_blocks()
    code_blocks = magic_model.get_code_blocks()
    ref_text_blocks = magic_model.get_ref_text_blocks()
    phonetic_blocks = magic_model.get_phonetic_blocks()
    list_blocks = magic_model.get_list_blocks()

    # 如果有标题优化需求，则对title_blocks截图det
    if heading_level_import_success:
        ocr_model = get_ch_lite_ocr_det_model()
        for title_block in title_blocks:
            ocr_det_res, _ = detect_ocr_boxes_from_padded_crop(
                title_block.get('bbox'),
                page_pil_img,
                scale,
                ocr_model=ocr_model,
            )
            if len(ocr_det_res) > 0:
                # 计算所有res的平均高度
                avg_height = np.mean([box[2][1] - box[0][1] for box in ocr_det_res])
                title_block['line_avg_height'] = round(avg_height/scale)

    text_blocks = magic_model.get_text_blocks()
    interline_equation_blocks = magic_model.get_interline_equation_blocks()

    all_spans = magic_model.get_all_spans()
    # 对image/table/chart/interline_equation的span截图
    # [自定义] 印章 span（sub_type="seal"）从白化前的原图（img_pil_orig）裁剪，
    #          保留真实红章；白化图只用于 VLM/OCR 推理，避免印章图块被抠成白色。
    for span in all_spans:
        if span["type"] in [ContentType.IMAGE, ContentType.TABLE, ContentType.CHART, ContentType.INTERLINE_EQUATION]:
            crop_src = (
                image_dict["img_pil_orig"]
                if (span.get("sub_type") == "seal" and image_dict.get("img_pil_orig") is not None)
                else page_pil_img
            )
            span = cut_image_and_table(span, crop_src, page_img_md5, page_index, image_writer, scale=scale)

    replace_inline_table_images(table_blocks, image_writer, page_index)

    page_blocks = []
    page_blocks.extend([
        *image_blocks,
        *table_blocks,
        *chart_blocks,
        *code_blocks,
        *ref_text_blocks,
        *phonetic_blocks,
        *title_blocks,
        *text_blocks,
        *interline_equation_blocks,
        *list_blocks,
    ])
    # 对page_blocks根据index的值进行排序
    page_blocks.sort(key=lambda x: x["index"])

    page_info = {
        "preproc_blocks": page_blocks,
        "discarded_blocks": discarded_blocks,
        "page_size": [width, height],
        "page_idx": page_index,
    }
    return page_info


def init_middle_json():
    return {"pdf_info": [], "_backend": "vlm", "_version_name": __version__}


def append_page_blocks_to_middle_json(
    middle_json,
    model_output_blocks_list,
    images_list,
    pdf_doc,
    image_writer,
    page_start_index=0,
    progress_bar=None,
):
    for offset, (page_blocks, image_dict) in enumerate(zip(model_output_blocks_list, images_list)):
        page_index = page_start_index + offset
        with pdfium_guard():
            page = pdf_doc[page_index]
        page_info = blocks_to_page_info(page_blocks, image_dict, page, image_writer, page_index)
        middle_json["pdf_info"].append(page_info)
        if progress_bar is not None:
            progress_bar.update(1)


def finalize_middle_json(pdf_info_list):
    # [自定义] 跨页表头一致性归一化（守卫 11 扩展版）
    # 必须早于 build_para_blocks_from_preproc 执行，
    # 否则 para_blocks 内表头结构不可逆。
    try:
        from mineru.utils.custom.config import get_table_header_consensus_enable
        if get_table_header_consensus_enable():
            from mineru.utils.custom.table_utils.header_consensus import normalize_table_headers_across_pages
            normalize_table_headers_across_pages(pdf_info_list)
    except Exception as exc:
        logger.warning(f"表头一致性归一化执行失败: {exc}")

    # [自定义] 金额千分位逗号被读成点号的确定性回写（VLM 原生缺陷）
    # 缺陷源自 VLM 出表，hybrid 与 VLM 两条后端都会复现，故保持同调，
    # 避免两条路由行为分叉（与上方表头一致性归一化同理）。
    # 必须在 build_para_blocks_from_preproc 之前执行，表 HTML 此后不可逆。
    try:
        from mineru.utils.custom.config import get_amount_sep_repair_enable
        if get_amount_sep_repair_enable():
            from mineru.utils.custom.table_utils.amount_sep_repair import (
                repair_dotted_amount_separators,
            )
            repair_dotted_amount_separators(pdf_info_list)
    except Exception as exc:
        logger.warning(f"金额千分位回写执行失败: {exc}")

    build_para_blocks_from_preproc(pdf_info_list)
    merge_para_text_blocks(pdf_info_list)

    table_enable = get_table_enable(os.getenv('MINERU_VLM_TABLE_ENABLE', 'True').lower() == 'true')
    if table_enable:
        cross_page_table_merge(pdf_info_list)

    if heading_level_import_success:
        llm_aided_title_start_time = time.time()
        llm_aided_title(pdf_info_list, title_aided_config)
        logger.info(f'llm aided title time: {round(time.time() - llm_aided_title_start_time, 2)}')

    cleanup_internal_para_block_metadata(pdf_info_list)


def result_to_middle_json(model_output_blocks_list, images_list, pdf_doc, image_writer):
    middle_json = init_middle_json()
    with tqdm(total=len(model_output_blocks_list), desc="Processing pages") as progress_bar:
        append_page_blocks_to_middle_json(
            middle_json,
            model_output_blocks_list,
            images_list,
            pdf_doc,
            image_writer,
            progress_bar=progress_bar,
        )

    finalize_middle_json(middle_json["pdf_info"])
    close_pdfium_document(pdf_doc)
    return middle_json
