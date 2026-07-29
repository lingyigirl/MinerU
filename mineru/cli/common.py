# Copyright (c) Opendatalab. All rights reserved.
import asyncio
import importlib
import importlib.util
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Sequence

from loguru import logger

from mineru.data.data_reader_writer import FileBasedDataWriter
from mineru.utils.draw_bbox import draw_layout_bbox, draw_span_bbox
from mineru.utils.engine_utils import get_vlm_engine
from mineru.utils.enum_class import MakeMode
from mineru.utils.guess_suffix_or_lang import guess_suffix_by_bytes
from mineru.utils.pdf_image_tools import images_bytes_to_pdf_bytes
from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make as vlm_union_make
from mineru.backend.office.office_middle_json_mkcontent import union_make as office_union_make
from mineru.backend.vlm.vlm_analyze import doc_analyze as vlm_doc_analyze
from mineru.backend.vlm.vlm_analyze import aio_doc_analyze as aio_vlm_doc_analyze
from mineru.backend.office.pptx_analyze import office_pptx_analyze
from mineru.backend.office.xlsx_analyze import office_xlsx_analyze
from mineru.backend.office.docx_analyze import office_docx_analyze
from mineru.utils.pdfium_guard import rewrite_pdf_bytes_with_pdfium

os.environ["TORCH_CUDNN_V8_API_DISABLED"] = "1"
if os.getenv("MINERU_LMDEPLOY_DEVICE", "") == "maca":
    import torch
    torch.backends.cudnn.enabled = False


pdf_suffixes = ["pdf"]
image_suffixes = ["png", "jpeg", "jp2", "webp", "gif", "bmp", "jpg", "tiff"]
docx_suffixes = ["docx"]
pptx_suffixes = ["pptx"]
xlsx_suffixes = ["xlsx"]
office_suffixes = docx_suffixes + pptx_suffixes + xlsx_suffixes

os.environ["TOKENIZERS_PARALLELISM"] = "false"
# Maximum UTF-8 byte length allowed for task stems used in filenames.
# 200 bytes is chosen to stay well below common filesystem limits (e.g. 255 bytes)
# and to prevent generating excessively long or incompatible filenames.
MAX_TASK_STEM_BYTES = 200


class HybridDependencyError(RuntimeError):
    pass


def build_hybrid_dependency_error_message(backend: str) -> str:
    return (
        f"`{backend}` requires local pipeline dependencies (`mineru[pipeline]`, "
        "including `torch`). Install `mineru[pipeline]` or `mineru[core]`. "
        "If you need a lightweight remote client without local `torch`, "
        "use `vlm-http-client` instead."
    )


def ensure_backend_dependencies(backend: str) -> None:
    if not backend.startswith("hybrid-"):
        return
    if importlib.util.find_spec("torch") is None:
        raise HybridDependencyError(build_hybrid_dependency_error_message(backend))


def _load_hybrid_analyze_entrypoint(entrypoint_name: str, backend: str):
    ensure_backend_dependencies(backend)
    try:
        hybrid_analyze = importlib.import_module("mineru.backend.hybrid.hybrid_analyze")
    except (ImportError, ModuleNotFoundError) as exc:
        raise HybridDependencyError(
            build_hybrid_dependency_error_message(backend)
        ) from exc
    return getattr(hybrid_analyze, entrypoint_name)


def utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_to_utf8_bytes(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError as exc:
            truncated = truncated[:exc.start]
    return ""


def normalize_task_stem(stem: str, max_bytes: int = MAX_TASK_STEM_BYTES) -> str:
    return truncate_to_utf8_bytes(stem, max_bytes)


def normalize_upload_filename(upload_name: str) -> str:
    sanitized_name = Path(upload_name).name
    sanitized_path = Path(sanitized_name)
    normalized_stem = normalize_task_stem(sanitized_path.stem)
    return f"{normalized_stem}{sanitized_path.suffix}"


def build_task_stem_candidate(
    stem: str,
    suffix: str = "",
    max_bytes: int = MAX_TASK_STEM_BYTES,
) -> str:
    if utf8_byte_length(f"{stem}{suffix}") <= max_bytes:
        return f"{stem}{suffix}"
    suffix_bytes = utf8_byte_length(suffix)
    if suffix_bytes >= max_bytes:
        return truncate_to_utf8_bytes(suffix, max_bytes)
    return f"{truncate_to_utf8_bytes(stem, max_bytes - suffix_bytes)}{suffix}"


def uniquify_task_stems(
    stems: Sequence[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Assign task-local unique stems while preserving input order."""
    normalized_inputs = [normalize_task_stem(stem) for stem in stems]
    raw_keys = {stem.casefold() for stem in normalized_inputs}
    occurrence_counts: dict[str, int] = {}
    assigned_keys: set[str] = set()
    unique_stems: list[str] = []
    renamed: list[tuple[str, str]] = []

    for stem, normalized_stem in zip(stems, normalized_inputs):
        stem_base = normalized_stem or stem
        stem_key = stem_base.casefold()
        seen_count = occurrence_counts.get(stem_key, 0)
        occurrence_counts[stem_key] = seen_count + 1

        if seen_count == 0 and stem_key not in assigned_keys:
            effective_stem = stem_base
        else:
            suffix = seen_count + 1
            while True:
                candidate = build_task_stem_candidate(stem_base, f"_{suffix}")
                candidate_key = candidate.casefold()
                if candidate_key not in raw_keys and candidate_key not in assigned_keys:
                    effective_stem = candidate
                    break
                suffix += 1

        assigned_keys.add(effective_stem.casefold())
        unique_stems.append(effective_stem)
        if effective_stem != stem:
            renamed.append((stem, effective_stem))

    return unique_stems, renamed


def read_fn(path, file_suffix: str | None = None):
    if not isinstance(path, Path):
        path = Path(path)
    with open(str(path), "rb") as input_file:
        file_bytes = input_file.read()
        if file_suffix is None:
            file_suffix = guess_suffix_by_bytes(file_bytes, path)
        if file_suffix in image_suffixes:
            return images_bytes_to_pdf_bytes(file_bytes)
        elif file_suffix in pdf_suffixes + office_suffixes:
            return file_bytes
        else:
            raise Exception(f"Unknown file suffix: {file_suffix}")


def prepare_env(output_dir, pdf_file_name, parse_method):
    local_md_dir = str(os.path.join(output_dir, pdf_file_name, parse_method))
    local_image_dir = os.path.join(str(local_md_dir), "images")
    os.makedirs(local_image_dir, exist_ok=True)
    os.makedirs(local_md_dir, exist_ok=True)
    return local_image_dir, local_md_dir


def convert_pdf_bytes_to_bytes(pdf_bytes, start_page_id=0, end_page_id=None):
    try:
        rebuilt_pdf_bytes = rewrite_pdf_bytes_with_pdfium(
            pdf_bytes,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
        )
        if rebuilt_pdf_bytes:
            return rebuilt_pdf_bytes
        logger.warning("PDFium rewrite returned empty bytes, using original PDF bytes.")
    except Exception as fallback_error:
        logger.warning(
            f"Error in converting PDF bytes with pdfium: {fallback_error}, "
            "using original PDF bytes."
        )
    return pdf_bytes


def _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id):
    """准备处理PDF字节数据"""
    result = []
    for pdf_bytes in pdf_bytes_list:
        new_pdf_bytes = convert_pdf_bytes_to_bytes(pdf_bytes, start_page_id, end_page_id)
        result.append(new_pdf_bytes)
    return result


def _process_output(
        pdf_info,
        pdf_bytes,
        pdf_file_name,
        local_md_dir,
        local_image_dir,
        md_writer,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_orig_pdf,
        f_dump_md,
        f_dump_content_list,
        f_dump_middle_json,
        f_dump_model_output,
        f_make_md_mode,
        middle_json,
        model_output=None,
        process_mode="vlm",
):
    from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
    if process_mode == "pipeline":
        make_func = pipeline_union_make
    elif process_mode == "vlm":
        make_func = vlm_union_make
    elif process_mode in office_suffixes:
        make_func = office_union_make
    else:
        raise Exception(f"Unknown process_mode: {process_mode}")
    """处理输出文件"""
    if f_draw_layout_bbox:
        try:
            draw_layout_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_layout.pdf")
        except Exception as exc:
            logger.warning(f"Skipping layout bbox visualization for {pdf_file_name}: {exc}")

    if f_draw_span_bbox:
        try:
            draw_span_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_span.pdf")
        except Exception as exc:
            logger.warning(f"Skipping span bbox visualization for {pdf_file_name}: {exc}")

    if f_dump_orig_pdf:
        if process_mode in ["pipeline", "vlm"]:
            md_writer.write(
                f"{pdf_file_name}_origin.pdf",
                pdf_bytes,
            )
            # [自定义] 生成旋转修正后的 PDF（页面朝向检测+旋转为正）
            # 合并上游时注意：此 hook 只依赖 mineru/utils/custom/ 下的自定义模块
            try:
                from mineru.utils.custom.pdf_utils import generate_rotation_corrected_pdf
                rotated_pdf_bytes = generate_rotation_corrected_pdf(pdf_bytes)
                if rotated_pdf_bytes:
                    md_writer.write(
                        f"{pdf_file_name}_rotated.pdf",
                        rotated_pdf_bytes,
                    )
                else:
                    logger.warning(
                        f"旋转修正PDF生成为空，跳过写入 {pdf_file_name}_rotated.pdf，"
                        f"请检查上方日志中是否有 load_images_from_pdf_core 或 pdf_images_to_pdf_bytes 的警告"
                    )
            except Exception as exc:
                logger.warning(
                    f"Skipping rotation-corrected PDF for {pdf_file_name}: {exc}"
                )
        elif process_mode in office_suffixes:
            md_writer.write(
                f"{pdf_file_name}_origin.{process_mode}",
                pdf_bytes,
            )

    image_dir = str(os.path.basename(local_image_dir))

    if f_dump_md:
        md_content_str = make_func(pdf_info, f_make_md_mode, image_dir)
        md_writer.write_string(
            f"{pdf_file_name}.md",
            md_content_str,
        )

    if f_dump_content_list:

        content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
        md_writer.write_string(
            f"{pdf_file_name}_content_list.json",
            json.dumps(content_list, ensure_ascii=False, indent=4),
        )

        content_list_v2 = make_func(pdf_info, MakeMode.CONTENT_LIST_V2, image_dir)
        md_writer.write_string(
            f"{pdf_file_name}_content_list_v2.json",
            json.dumps(content_list_v2, ensure_ascii=False, indent=4),
        )


    if f_dump_middle_json:
        md_writer.write_string(
            f"{pdf_file_name}_middle.json",
            json.dumps(middle_json, ensure_ascii=False, indent=4),
        )

    if f_dump_model_output:
        md_writer.write_string(
            f"{pdf_file_name}_model.json",
            json.dumps(model_output, ensure_ascii=False, indent=4),
        )

    logger.debug(f"local output dir is {local_md_dir}")


def _process_pipeline(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        p_lang_list,
        parse_method,
        p_formula_enable,
        p_table_enable,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
):
    """处理pipeline后端逻辑"""
    from mineru.backend.pipeline.pipeline_analyze import doc_analyze_streaming as pipeline_doc_analyze_streaming

    image_writer_list = []
    md_writer_list = []
    local_output_info = []
    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)
        image_writer_list.append(image_writer)
        md_writer_list.append(md_writer)
        local_output_info.append((pdf_file_name, local_image_dir, local_md_dir))

    output_futures = []

    def run_output_task(doc_index, middle_json, model_list):
        pdf_file_name, local_image_dir, local_md_dir = local_output_info[doc_index]
        md_writer = md_writer_list[doc_index]
        pdf_bytes = pdf_bytes_list[doc_index]
        logger.debug(f"Pipeline output start: doc{doc_index}")
        try:
            _process_output(
                middle_json["pdf_info"], pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
                md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
                f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
                f_make_md_mode, middle_json, model_list, process_mode="pipeline"
            )
            logger.debug(f"Pipeline output complete: doc{doc_index}")
        except Exception:
            logger.exception(f"Pipeline output failed: doc{doc_index}")
            raise

    with ThreadPoolExecutor(max_workers=1) as output_executor:
        def on_doc_ready(doc_index, model_list, middle_json, ocr_enable):
            logger.debug(
                f"Pipeline doc ready: doc{doc_index} pages={len(middle_json['pdf_info'])} output_submitted=1"
            )
            future = output_executor.submit(run_output_task, doc_index, middle_json, model_list)
            output_futures.append(future)

        pipeline_doc_analyze_streaming(
            pdf_bytes_list,
            image_writer_list,
            p_lang_list,
            on_doc_ready,
            parse_method=parse_method,
            formula_enable=p_formula_enable,
            table_enable=p_table_enable,
        )

        for future in output_futures:
            future.result()
    return


async def _async_process_vlm(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        **kwargs,
):
    """异步处理VLM后端逻辑"""
    parse_method = "vlm"
    f_draw_span_bbox = False
    if not backend.endswith("client"):
        server_url = None

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = await aio_vlm_doc_analyze(
            pdf_bytes, image_writer=image_writer, backend=backend, server_url=server_url, **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


def _process_vlm(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        **kwargs,
):
    """同步处理VLM后端逻辑"""
    parse_method = "vlm"
    f_draw_span_bbox = False
    if not backend.endswith("client"):
        server_url = None

    for idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result = vlm_doc_analyze(
            pdf_bytes, image_writer=image_writer, backend=backend, server_url=server_url, **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


def _process_hybrid(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        h_lang_list,
        parse_method,
        inline_formula_enable,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        **kwargs,
):
    hybrid_doc_analyze = _load_hybrid_analyze_entrypoint(
        "doc_analyze",
        f"hybrid-{backend}",
    )
    """同步处理hybrid后端逻辑"""
    if not backend.endswith("client"):
        server_url = None

    for idx, (pdf_bytes, lang) in enumerate(zip(pdf_bytes_list, h_lang_list)):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, f"hybrid_{parse_method}")
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result, _vlm_ocr_enable = hybrid_doc_analyze(
            pdf_bytes,
            image_writer=image_writer,
            backend=backend,
            parse_method=parse_method,
            language=lang,
            inline_formula_enable=inline_formula_enable,
            server_url=server_url,
            **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        # f_draw_span_bbox = not _vlm_ocr_enable
        f_draw_span_bbox = False

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


async def _async_process_hybrid(
        output_dir,
        pdf_file_names,
        pdf_bytes_list,
        h_lang_list,
        parse_method,
        inline_formula_enable,
        backend,
        f_draw_layout_bbox,
        f_draw_span_bbox,
        f_dump_md,
        f_dump_middle_json,
        f_dump_model_output,
        f_dump_orig_pdf,
        f_dump_content_list,
        f_make_md_mode,
        server_url=None,
        **kwargs,
):
    aio_hybrid_doc_analyze = _load_hybrid_analyze_entrypoint(
        "aio_doc_analyze",
        f"hybrid-{backend}",
    )
    """异步处理hybrid后端逻辑"""
    if not backend.endswith("client"):
        server_url = None

    for idx, (pdf_bytes, lang) in enumerate(zip(pdf_bytes_list, h_lang_list)):
        pdf_file_name = pdf_file_names[idx]
        local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, f"hybrid_{parse_method}")
        image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

        middle_json, infer_result, _vlm_ocr_enable = await aio_hybrid_doc_analyze(
            pdf_bytes,
            image_writer=image_writer,
            backend=backend,
            parse_method=parse_method,
            language=lang,
            inline_formula_enable=inline_formula_enable,
            server_url=server_url,
            **kwargs,
        )

        pdf_info = middle_json["pdf_info"]

        # f_draw_span_bbox = not _vlm_ocr_enable
        f_draw_span_bbox = False

        _process_output(
            pdf_info, pdf_bytes, pdf_file_name, local_md_dir, local_image_dir,
            md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_pdf,
            f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
            f_make_md_mode, middle_json, infer_result, process_mode="vlm"
        )


def _process_office_doc(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_file=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
):
    need_remove_index = []
    for i, file_bytes in enumerate(pdf_bytes_list):
        pdf_file_name = pdf_file_names[i]
        file_suffix = guess_suffix_by_bytes(file_bytes)
        if file_suffix in office_suffixes:

            need_remove_index.append(i)

            local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, f"office")
            image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)

            if file_suffix in docx_suffixes:
                office_analyze = office_docx_analyze
            elif file_suffix in pptx_suffixes:
                office_analyze = office_pptx_analyze
            elif file_suffix in xlsx_suffixes:
                office_analyze = office_xlsx_analyze
            else:
                raise ValueError(f"Unsupported office suffix: {file_suffix}")

            middle_json, infer_result = office_analyze(
                file_bytes,
                image_writer=image_writer,
            )

            f_draw_layout_bbox = False
            f_draw_span_bbox = False
            pdf_info = middle_json["pdf_info"]

            _process_output(
                pdf_info, file_bytes, pdf_file_name, local_md_dir, local_image_dir,
                md_writer, f_draw_layout_bbox, f_draw_span_bbox, f_dump_orig_file,
                f_dump_md, f_dump_content_list, f_dump_middle_json, f_dump_model_output,
                f_make_md_mode, middle_json, infer_result, process_mode=file_suffix
            )

    return need_remove_index


# [自定义] S0 → S1 智能路由系统
# 合并上游时注意：此函数组只依赖 mineru/utils/custom/ 下的自定义模块


def _process_form_kvp(
    output_dir: str,
    pdf_file_name: str,
    pdf_bytes: bytes,
    kvp_engine: str,
    kvp_server_url: str | None,
    f_dump_md: bool,
    f_dump_middle_json: bool,
    f_dump_model_output: bool,
    f_dump_content_list: bool,
    f_make_md_mode,
    kvp_verify_engine: str | None = None,
) -> None:
    """使用 KVP Pipeline 处理票据/卡证文档并生成输出。

    生成与 MinerU 兼容的输出格式：
    - {name}.md：KVP 字段表格（Markdown）
    - {name}_middle.json：包含 KVP 数据和页面信息
    - {name}_content_list.json / _v2.json：内容列表
    - images/：页面截图（用于可视化验证）

    Args:
        output_dir: 输出根目录。
        pdf_file_name: PDF 文件名（不含后缀）。
        pdf_bytes: PDF 字节流。
        kvp_engine: KVP 引擎标识符。
        kvp_server_url: 自定义 API 地址。
        f_dump_md: 是否输出 Markdown。
        f_dump_middle_json: 是否输出 middle_json。
        f_dump_model_output: 是否输出 model_output。
        f_dump_content_list: 是否输出 content_list。
        f_make_md_mode: Markdown 生成模式。
    """
    try:
        from mineru.utils.custom.kvp_extractor import extract_kvp_from_form
    except (ImportError, ModuleNotFoundError) as exc:
        logger.warning(f"KVP 提取模块不可用，跳过: {exc}")
        return

    parse_method = "kvp"
    local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, parse_method)
    _image_writer = FileBasedDataWriter(local_image_dir)
    md_writer = FileBasedDataWriter(local_md_dir)

    try:
        middle_json = extract_kvp_from_form(
            pdf_bytes,
            engine=kvp_engine,
            server_url=kvp_server_url,
            verify_engine=kvp_verify_engine,
        )
    except Exception:
        logger.exception(f"KVP 提取失败: {pdf_file_name}")
        return

    pdf_info = middle_json.get("pdf_info", [])

    # ---- 渲染页面截图到 images/（对齐 MinerU 行为） ----
    try:
        from mineru.utils.pdf_image_tools import load_images_from_pdf_core

        page_images = load_images_from_pdf_core(pdf_bytes)
        for page_idx, img_dict in enumerate(page_images):
            if page_idx >= len(pdf_info):
                break
            pil_img = img_dict["img_pil"]
            # 使用 MinerU 兼容的文件名格式
            img_filename = f"{pdf_file_name}_{page_idx:04d}.jpg"
            img_bytes_io = BytesIO()
            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")
            pil_img.save(img_bytes_io, format="JPEG", quality=92)
            _image_writer.write(img_filename, img_bytes_io.getvalue())
    except Exception:
        logger.warning(f"页面截图保存失败: {pdf_file_name}")

    # ---- 生成 Markdown（对齐 MinerU 的 MM_MD 格式） ----
    if f_dump_md:
        md_content = _make_kvp_markdown(pdf_info, pdf_file_name, f_make_md_mode)
        md_writer.write_string(f"{pdf_file_name}.md", md_content)

    # ---- 生成 content_list（对齐 MinerU 格式） ----
    if f_dump_content_list:
        content_list = _make_kvp_content_list(pdf_info, pdf_file_name)
        md_writer.write_string(
            f"{pdf_file_name}_content_list.json",
            json.dumps(content_list, ensure_ascii=False, indent=4),
        )
        # content_list_v2 复用同一结构（KVP 输出无 block 层级差异）
        md_writer.write_string(
            f"{pdf_file_name}_content_list_v2.json",
            json.dumps(content_list, ensure_ascii=False, indent=4),
        )

    # ---- 输出 middle_json ----
    if f_dump_middle_json:
        md_writer.write_string(
            f"{pdf_file_name}_middle.json",
            json.dumps(middle_json, ensure_ascii=False, indent=4),
        )

    # ---- 输出 model_output（KVP 原始响应） ----
    if f_dump_model_output:
        md_writer.write_string(
            f"{pdf_file_name}_model.json",
            json.dumps(middle_json, ensure_ascii=False, indent=4),
        )

    logger.info(f"KVP 处理完成: {pdf_file_name}")


# ---------------------------------------------------------------------------
# KVP → MinerU 输出格式转换
# ---------------------------------------------------------------------------

def _make_kvp_markdown(
    pdf_info: list[dict],
    pdf_file_name: str,
    f_make_md_mode,
) -> str:
    """从 KVP middle_json 生成 Markdown（对齐 MinerU 的 MM_MD / NLP_MD 格式）。

    输出格式：
    - MM_MD：每个字段一行 `**键**: 值`
    - NLP_MD：纯文本段落

    Args:
        pdf_info: KVP middle_json 中的 pdf_info 列表。
        pdf_file_name: PDF 文件名。
        f_make_md_mode: Markdown 生成模式。

    Returns:
        Markdown 字符串。
    """
    lines = [f"# {pdf_file_name}\n"]

    # f_make_md_mode 可能是 MakeMode 枚举，也可能已被转换为字符串
    mode_str = f_make_md_mode.value if hasattr(f_make_md_mode, "value") else str(f_make_md_mode)

    for page in pdf_info:
        page_idx = page.get("page_idx", 0)
        kvp_raw = page.get("_kvp_raw", {})

        if not kvp_raw:
            spans = page.get("spans", [])
            for span in spans:
                if span.get("type") == "text":
                    text = span.get("text", "")
                    if text:
                        for line in text.split("\n"):
                            if ":" in line:
                                lines.append(line)
                            else:
                                lines.append(line)
            continue

        if mode_str == "nlp_markdown":
            # 自然语言格式：每行一个 KVP
            for key, value in kvp_raw.items():
                if key.startswith("_"):
                    continue
                lines.append(f"{key}: {value}")
        else:
            # MM_MD 格式：表格
            if page_idx == 0:
                lines.append("| 字段 | 值 |")
                lines.append("|------|-----|")
            for key, value in kvp_raw.items():
                if key.startswith("_"):
                    continue
                # 转义 Markdown 表格中的特殊字符
                safe_value = str(value).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {key} | {safe_value} |")

        lines.append("")

    return "\n".join(lines)


def _make_kvp_content_list(
    pdf_info: list[dict],
    pdf_file_name: str,
) -> list[dict]:
    """从 KVP middle_json 生成 content_list（对齐 MinerU 格式）。

    每页生成一个 "kvp_form" 类型的 block，包含所有提取的 KVP 字段。

    Args:
        pdf_info: KVP middle_json 中的 pdf_info 列表。
        pdf_file_name: PDF 文件名。

    Returns:
        content_list 格式的列表。
    """
    content_list = []
    for page in pdf_info:
        page_idx = page.get("page_idx", 0)
        kvp_raw = page.get("_kvp_raw", {})

        # 过滤元数据字段
        fields = {k: v for k, v in kvp_raw.items() if not k.startswith("_")}

        block = {
            "type": "kvp_form",
            "page_idx": page_idx,
            "fields": fields,
            "field_count": len(fields),
            "source": page.get("spans", [{}])[0].get("source", "kvp") if page.get("spans") else "kvp",
        }
        content_list.append(block)

    return content_list


def _try_smart_routing(
    pdf_file_names: list[str],
    pdf_bytes_list: list[bytes],
    p_lang_list: list[str],
    output_dir: str,
    backend: str,
    parse_method: str,
    doc_type: str,
    kvp_engine: str,
    kvp_server_url: str | None,
    f_draw_layout_bbox: bool,
    f_draw_span_bbox: bool,
    f_dump_md: bool,
    f_dump_middle_json: bool,
    f_dump_model_output: bool,
    f_dump_orig_pdf: bool,
    f_dump_content_list: bool,
    f_make_md_mode,
    formula_enable: bool,
    table_enable: bool,
    image_analysis: bool,
    server_url: str | None,
    start_page_id: int,
    end_page_id: int | None,
    kvp_verify_engine: str | None = None,
    **kwargs,
) -> bool:
    """S0 → S1 → KVP 智能路由入口。

    返回 True 表示请求已被处理（路由到 KVP Pipeline），调用者应 return。
    返回 False 表示请求未被处理，调用者继续原有逻辑。

    Args:
        pdf_file_names: PDF 文件名列表。
        pdf_bytes_list: PDF 字节流列表。
        ... (其他参数透传自 do_parse / aio_do_parse)

    Returns:
        bool: True = 已处理（调用者应 return），False = 未处理（继续原有逻辑）。
    """
    # 仅处理第一个 PDF（多文件场景暂不支持混合路由）
    if len(pdf_bytes_list) != 1:
        if doc_type == "form_kvp":
            logger.warning("KVP 模式仅支持单文件，将使用通用解析")
        return False

    pdf_bytes = pdf_bytes_list[0]
    pdf_file_name = pdf_file_names[0]

    # 用户强制 KVP 模式
    if doc_type == "form_kvp":
        logger.info(f"[自定义] 用户指定 KVP 模式: engine={kvp_engine}")
        _process_form_kvp(
            output_dir=output_dir,
            pdf_file_name=pdf_file_name,
            pdf_bytes=pdf_bytes,
            kvp_engine=kvp_engine,
            kvp_server_url=kvp_server_url,
            f_dump_md=f_dump_md,
            f_dump_middle_json=f_dump_middle_json,
            f_dump_model_output=f_dump_model_output,
            f_dump_content_list=f_dump_content_list,
            f_make_md_mode=f_make_md_mode,
            kvp_verify_engine=kvp_verify_engine,
        )
        return True

    # doc_type == "auto" → 运行 S0 + S1 自动分类（用户显式选择，需付出分析开销）
    if doc_type == "auto":
        try:
            from mineru.utils.custom.doc_quality import analyze_document_quality
            from mineru.utils.custom.doc_classifier import classify_document, DocType
            from mineru.utils.custom.engine_factory import select_engine_route, make_routing_summary

            # S0: 质量分析
            quality = analyze_document_quality(pdf_bytes)

            # S1: 文档分类
            doc_type_result = classify_document(pdf_bytes, quality=quality)

            # 选择引擎路由
            route = select_engine_route(
                doc_type=doc_type_result,
                quality=quality,
                user_kvp_engine=kvp_engine,
                user_kvp_server_url=kvp_server_url,
                user_kvp_verify_engine=kvp_verify_engine,
            )

            # 生成路由摘要（日志用）
            make_routing_summary(doc_type_result, route, quality)

            # 如果是 KVP 路由 → 处理
            if route.engine_backend == "kvp":
                logger.info(
                    f"[自定义] 智能路由: {doc_type_result.value} → KVP Pipeline"
                )
                _process_form_kvp(
                    output_dir=output_dir,
                    pdf_file_name=pdf_file_name,
                    pdf_bytes=pdf_bytes,
                    kvp_engine=route.extra_kwargs.get("kvp_engine", kvp_engine),
                    kvp_server_url=route.extra_kwargs.get("kvp_server_url", kvp_server_url),
                    f_dump_md=f_dump_md,
                    f_dump_middle_json=f_dump_middle_json,
                    f_dump_model_output=f_dump_model_output,
                    f_dump_content_list=f_dump_content_list,
                    f_make_md_mode=f_make_md_mode,
                    kvp_verify_engine=route.extra_kwargs.get("kvp_verify_engine", kvp_verify_engine),
                )
                return True

            # 非 KVP 路由 → 透传，继续原有逻辑
            logger.info(
                f"[自定义] 智能路由: {doc_type_result.value} → {route.engine_backend}"
            )
            return False

        except Exception:
            logger.exception("[自定义] 智能路由失败，回退到原有解析流程")
            return False

    # doc_type == "general" → 透传
    return False


def do_parse(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        backend="pipeline",
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
        server_url=None,
        f_draw_layout_bbox=True,
        f_draw_span_bbox=True,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_pdf=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        start_page_id=0,
        end_page_id=None,
        image_analysis=True,
        # [自定义] 多引擎路由参数
        doc_type: str = "auto",
        kvp_engine: str | None = None,
        kvp_server_url: str | None = None,
        kvp_verify_engine: str | None = None,
        **kwargs,
):
    need_remove_index = _process_office_doc(
        output_dir,
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
    )
    for index in sorted(need_remove_index, reverse=True):
        del pdf_bytes_list[index]
        del pdf_file_names[index]
        del p_lang_list[index]
    if not pdf_bytes_list:
        logger.warning("No valid PDF or image files to process.")
        return

    # 预处理PDF字节数据
    pdf_bytes_list = _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id)

    # [自定义] S0 → S1 智能路由 hook
    # 合并上游时注意：此 hook 只依赖 mineru/utils/custom/ 下的自定义模块
    _routed = _try_smart_routing(
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        p_lang_list=p_lang_list,
        output_dir=output_dir,
        backend=backend,
        parse_method=parse_method,
        doc_type=doc_type,
        kvp_engine=kvp_engine,
        kvp_server_url=kvp_server_url,
        f_draw_layout_bbox=f_draw_layout_bbox,
        f_draw_span_bbox=f_draw_span_bbox,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_pdf=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
        formula_enable=formula_enable,
        table_enable=table_enable,
        image_analysis=image_analysis,
        server_url=server_url,
        start_page_id=start_page_id,
        end_page_id=end_page_id,
        kvp_verify_engine=kvp_verify_engine,
        **kwargs,
    )
    if _routed:
        return

    if backend == "pipeline":
        _process_pipeline(
            output_dir, pdf_file_names, pdf_bytes_list, p_lang_list,
            parse_method, formula_enable, table_enable,
            f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
            f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode
        )
    else:
        if backend.startswith("vlm-"):
            backend = backend[4:]

            if backend == "vllm-async-engine":
                raise Exception("vlm-vllm-async-engine backend is not supported in sync mode, please use vlm-vllm-engine backend")

            if backend == "auto-engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=False)

            os.environ['MINERU_VLM_FORMULA_ENABLE'] = str(formula_enable)
            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)

            _process_vlm(
                output_dir, pdf_file_names, pdf_bytes_list, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, image_analysis=image_analysis, **kwargs,
            )
        elif backend.startswith("hybrid-"):
            ensure_backend_dependencies(backend)
            backend = backend[7:]

            if backend == "vllm-async-engine":
                raise Exception(
                    "hybrid-vllm-async-engine backend is not supported in sync mode, please use hybrid-vllm-engine backend")

            if backend == "auto-engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=False)

            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)
            os.environ['MINERU_VLM_FORMULA_ENABLE'] = "true"

            _process_hybrid(
                output_dir, pdf_file_names, pdf_bytes_list, p_lang_list, parse_method, formula_enable, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, image_analysis=image_analysis, **kwargs,
            )


async def aio_do_parse(
        output_dir,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        backend="pipeline",
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
        server_url=None,
        f_draw_layout_bbox=True,
        f_draw_span_bbox=True,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=True,
        f_dump_orig_pdf=True,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        start_page_id=0,
        end_page_id=None,
        image_analysis=True,
        # [自定义] 多引擎路由参数
        doc_type: str = "auto",
        kvp_engine: str | None = None,
        kvp_server_url: str | None = None,
        kvp_verify_engine: str | None = None,
        **kwargs,
):
    # Office 解析是同步且可能耗时的操作，异步入口需要放到线程中避免阻塞事件循环。
    need_remove_index = await asyncio.to_thread(
        _process_office_doc,
        output_dir,
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_file=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
    )
    for index in sorted(need_remove_index, reverse=True):
        del pdf_bytes_list[index]
        del pdf_file_names[index]
        del p_lang_list[index]
    if not pdf_bytes_list:
        logger.warning("No valid PDF or image files to process.")
        return

    # 预处理PDF字节数据
    pdf_bytes_list = _prepare_pdf_bytes(pdf_bytes_list, start_page_id, end_page_id)

    # [自定义] S0 → S1 智能路由 hook（异步路径）
    _routed = _try_smart_routing(
        pdf_file_names=pdf_file_names,
        pdf_bytes_list=pdf_bytes_list,
        p_lang_list=p_lang_list,
        output_dir=output_dir,
        backend=backend,
        parse_method=parse_method,
        doc_type=doc_type,
        kvp_engine=kvp_engine,
        kvp_server_url=kvp_server_url,
        f_draw_layout_bbox=f_draw_layout_bbox,
        f_draw_span_bbox=f_draw_span_bbox,
        f_dump_md=f_dump_md,
        f_dump_middle_json=f_dump_middle_json,
        f_dump_model_output=f_dump_model_output,
        f_dump_orig_pdf=f_dump_orig_pdf,
        f_dump_content_list=f_dump_content_list,
        f_make_md_mode=f_make_md_mode,
        formula_enable=formula_enable,
        table_enable=table_enable,
        image_analysis=image_analysis,
        server_url=server_url,
        start_page_id=start_page_id,
        end_page_id=end_page_id,
        kvp_verify_engine=kvp_verify_engine,
        **kwargs,
    )
    if _routed:
        return

    if backend == "pipeline":
        # pipeline模式暂不支持异步，使用同步处理方式
        _process_pipeline(
            output_dir, pdf_file_names, pdf_bytes_list, p_lang_list,
            parse_method, formula_enable, table_enable,
            f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
            f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode
        )
    else:
        if backend.startswith("vlm-"):
            backend = backend[4:]

            if backend == "vllm-engine":
                raise Exception("vlm-vllm-engine backend is not supported in async mode, please use vlm-vllm-async-engine backend")

            if backend == "auto-engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=True)

            os.environ['MINERU_VLM_FORMULA_ENABLE'] = str(formula_enable)
            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)

            await _async_process_vlm(
                output_dir, pdf_file_names, pdf_bytes_list, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, image_analysis=image_analysis, **kwargs,
            )
        elif backend.startswith("hybrid-"):
            ensure_backend_dependencies(backend)
            backend = backend[7:]

            if backend == "vllm-engine":
                raise Exception("hybrid-vllm-engine backend is not supported in async mode, please use hybrid-vllm-async-engine backend")

            if backend == "auto-engine":
                backend = get_vlm_engine(inference_engine='auto', is_async=True)

            os.environ['MINERU_VLM_TABLE_ENABLE'] = str(table_enable)
            os.environ['MINERU_VLM_FORMULA_ENABLE'] = "true"

            await _async_process_hybrid(
                output_dir, pdf_file_names, pdf_bytes_list, p_lang_list, parse_method, formula_enable, backend,
                f_draw_layout_bbox, f_draw_span_bbox, f_dump_md, f_dump_middle_json,
                f_dump_model_output, f_dump_orig_pdf, f_dump_content_list, f_make_md_mode,
                server_url, image_analysis=image_analysis, **kwargs,
            )


if __name__ == "__main__":
    # pdf_path = "../../demo/pdfs/demo3.pdf"
    pdf_path = "C:/Users/zhaoxiaomeng/Downloads/4546d0e2-ba60-40a5-a17e-b68555cec741.pdf"

    try:
       do_parse("./output", [Path(pdf_path).stem], [read_fn(Path(pdf_path))],["ch"],
                end_page_id=10,
                backend='vlm-huggingface'
                # backend = 'pipeline'
                )
    except Exception as e:
        logger.exception(e)
