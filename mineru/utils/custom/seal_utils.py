"""印章 OCR 补充模块。

在 Hybrid 模式下，VLM image_analysis 对印章文字的 OCR 精度不如
Pipeline 专用印章 OCR 模型（seal_crop.py + pytorch_paddle lang="seal"）。
本模块在 Hybrid 模式的后处理阶段，对已被 VLM 识别为 seal 的图像块
运行 Pipeline 印章 OCR，并根据置信度决定是否覆盖 VLM 结果。

[自定义] 此模块是 Fork 项目新增的自定义代码，上游无此模块。
合并上游时无需关注此文件，但需保留 hook 调用点。
"""

import os

import cv2
import numpy as np
from loguru import logger

from mineru.backend.pipeline.model_init import AtomModelSingleton
from mineru.backend.pipeline.model_list import AtomicModel
from mineru.backend.utils.para_block_utils import iter_block_spans
from mineru.utils.enum_class import ContentType

# Pipeline 印章 OCR 置信度阈值（0~1），低于此值保留 VLM 结果
_SEAL_OCR_CONFIDENCE_THRESHOLD = 0.8

# 印章 OCR 结果为空时的置信度（表示 OCR 完全无法识别，保留 VLM）
_SEAL_OCR_EMPTY_CONFIDENCE = 0.0


def _normalize_text(text: str) -> str:
    """规范化文本用于比较：去空格、统一全角半角。"""
    return text.replace(" ", "").replace("\n", "").replace("\r", "").strip()


def _load_seal_ocr_model():
    """懒加载 Pipeline 专用印章 OCR 模型（单例）。

    印章 OCR 使用 lang="seal" 参数，与通用文本 OCR 分离，
    包含弧形文字矫正裁剪（seal_crop.py）和专用识别模型。

    Returns:
        PytorchPaddleOCR 实例，配置为印章识别模式。
    """
    atom_model_manager = AtomModelSingleton()
    return atom_model_manager.get_atom_model(
        atom_model_name=AtomicModel.OCR,
        lang="seal",
    )


def _run_pipeline_seal_ocr(seal_img_bgr, seal_ocr_model):
    """对印章图片运行 Pipeline 专用印章 OCR。

    与 batch_analyze.py 中的印章 OCR 流程一致：
    1. det=True 进行印章文字区域检测（含弧形矫正）
    2. rec=True 对检测到的文字区域进行识别
    3. 返回 (texts, avg_score)：识别的文字列表和平均置信度

    Args:
        seal_img_bgr: BGR 格式的印章裁剪图片（numpy array）。
        seal_ocr_model: Pipeline 印章 OCR 模型实例。

    Returns:
        tuple[list[str], float]: (识别文字列表, 平均置信度)。
            如果识别失败返回 ([], 0.0)。
    """
    try:
        ocr_output = seal_ocr_model.ocr(seal_img_bgr, det=True, rec=True)
    except Exception:
        logger.exception("Pipeline 印章 OCR 执行异常")
        return [], _SEAL_OCR_EMPTY_CONFIDENCE

    if not ocr_output or not ocr_output[0]:
        return [], _SEAL_OCR_EMPTY_CONFIDENCE

    seal_texts = []
    scores = []

    for seal_item in ocr_output[0]:
        if not seal_item or len(seal_item) != 2:
            continue
        rec_result = seal_item[1]
        if not rec_result or len(rec_result) < 2:
            continue
        rec_text = rec_result[0]
        rec_score = float(rec_result[1])
        if rec_text:
            seal_texts.append(rec_text)
            scores.append(rec_score)

    if not seal_texts:
        return [], _SEAL_OCR_EMPTY_CONFIDENCE

    avg_score = np.mean(scores)
    return seal_texts, avg_score


def _load_seal_image(span, image_writer=None):
    """从 span 的 image_path 加载印章图片。

    Args:
        span: 中间 JSON 中的 image span。
        image_writer: 可选的 FileBasedDataWriter，用于解析相对路径。

    Returns:
        numpy array (BGR) 或 None。
    """
    image_path = span.get("image_path", "")
    if not image_path:
        return None

    seal_img = cv2.imread(image_path)
    if seal_img is None:
        # 尝试通过 image_writer 的根目录解析完整路径
        if image_writer is not None and hasattr(image_writer, '_parent_dir'):
            full_path = os.path.join(image_writer._parent_dir, image_path)
            seal_img = cv2.imread(full_path)
    return seal_img


def supplement_vlm_seal_with_ocr(
    pdf_info_list: list,
    hybrid_pipeline_model=None,
    image_writer=None,
) -> None:
    """使用 Pipeline 专用印章 OCR 补充/修正 VLM image_analysis 的印章文字。

    遍历所有 image span，对 VLM 识别为 seal（sub_type="seal"）的块：
    1. 从已保存的裁剪图片加载印章图像
    2. 运行 Pipeline 印章 OCR（含弧形矫正）
    3. 比较 VLM 和 OCR 结果：
       - 结果一致 → 高置信，不修改
       - 结果不一致 + OCR 置信度 >= 阈值 → 使用 OCR 结果覆盖
       - 结果不一致 + OCR 置信度 < 阈值 → 保留 VLM 结果
       - OCR 无结果 → 保留 VLM 结果

    [自定义] 此函数由 hybrid_model_output_to_middle_json.py 中的 hook 调用。

    Args:
        pdf_info_list: 中间 JSON 的页面列表（含 preproc_blocks）。
        hybrid_pipeline_model: Hybrid pipeline 模型实例（保留参数兼容性，当前未使用）。
        image_writer: 可选的 FileBasedDataWriter，用于解析图片相对路径。
    """
    seal_ocr_model = None
    supplement_count = 0
    skip_count = 0
    agree_count = 0

    for page_info in pdf_info_list:
        for block in page_info.get("preproc_blocks", []):
            # 只处理 sub_type 为 "seal" 的块
            if block.get("sub_type") != "seal":
                continue

            for span in iter_block_spans(block):
                if span.get("type") != ContentType.IMAGE:
                    continue

                vlm_content = span.get("content", "")
                if not vlm_content:
                    # VLM 未给出印章文字，仍尝试 OCR
                    pass

                # 加载印章图片
                seal_img = _load_seal_image(span, image_writer=image_writer)
                if seal_img is None:
                    skip_count += 1
                    logger.debug(
                        f"印章图片加载失败，跳过 OCR 补充: "
                        f"path={span.get('image_path', 'unknown')}"
                    )
                    continue

                # 懒加载印章 OCR 模型
                if seal_ocr_model is None:
                    try:
                        seal_ocr_model = _load_seal_ocr_model()
                    except Exception:
                        logger.exception("印章 OCR 模型加载失败，跳过印章 OCR 补充")
                        return

                # 运行 Pipeline 印章 OCR
                ocr_texts, ocr_confidence = _run_pipeline_seal_ocr(
                    seal_img, seal_ocr_model
                )

                if not ocr_texts:
                    # OCR 完全无法识别，保留 VLM 结果
                    skip_count += 1
                    logger.debug(
                        f"印章 OCR 无结果，保留 VLM 文字: "
                        f"vlm=\"{vlm_content}\""
                    )
                    continue

                # 将 OCR 识别的多个文本片段连接为一行
                ocr_content = "".join(ocr_texts)

                # 比较 VLM 和 OCR 结果
                vlm_norm = _normalize_text(vlm_content)
                ocr_norm = _normalize_text(ocr_content)

                if vlm_norm == ocr_norm:
                    # 结果一致，无需覆盖
                    agree_count += 1
                    continue

                # 结果不一致，根据 OCR 置信度决定
                if ocr_confidence >= _SEAL_OCR_CONFIDENCE_THRESHOLD:
                    # OCR 置信度不足，保留 VLM 结果
                    span["content"] = ocr_content
                    supplement_count += 1
                    logger.info(
                        f"印章 OCR 覆盖 VLM 结果: "
                        f"vlm=\"{vlm_content}\" → ocr=\"{ocr_content}\" "
                        f"(置信度={ocr_confidence:.3f}, "
                        f"ocr_texts={ocr_texts}, "
                        f"img={span.get('image_path', 'unknown').split('/')[-1]})"
                    )
                else:
                    # OCR 置信度高，使用 OCR 结果覆盖 VLM
                    skip_count += 1
                    logger.info(
                        f"印章 OCR 置信度不足，保留 VLM 结果: "
                        f"vlm=\"{vlm_content}\", ocr=\"{ocr_content}\" "
                        f"(置信度={ocr_confidence:.3f}, "
                        f"img={span.get('image_path', 'unknown').split('/')[-1]})"
                    )

    if supplement_count > 0 or skip_count > 0 or agree_count > 0:
        logger.info(
            f"印章 OCR 补充完成: "
            f"覆盖={supplement_count}, "
            f"保留VLM={skip_count}, "
            f"一致={agree_count}"
        )
