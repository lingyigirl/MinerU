"""自定义 PDF 处理工具。

此模块封装团队对 MinerU PDF 处理的扩展功能，
与上游 pdf_image_tools.py 解耦，便于合并上游更新。
"""

from mineru.utils.enum_class import ImageType
from mineru.utils.pdf_image_tools import (
    DEFAULT_PDF_IMAGE_DPI,
    image_rotate,
    load_images_from_pdf_core,
    pdf_images_to_pdf_bytes,
)


def generate_rotation_corrected_pdf(pdf_bytes: bytes, dpi: int = DEFAULT_PDF_IMAGE_DPI) -> bytes:
    """将 PDF 所有页面渲染并做朝向检测+旋转修正，合成为一个方向正确的 PDF。

    流程：
    1. pypdfium2 逐页渲染为 PIL Image
    2. image_rotate() 对每页做 4 方向分类 + 旋转修正（含混合朝向页面的局部修正）
    3. 将修正后的所有页面合成为单 PDF

    注意：此函数会用 pypdfium2 打开文档，调用者需确保 pdfium_guard 锁可用。
    """
    from loguru import logger

    try:
        images_list = load_images_from_pdf_core(
            pdf_bytes,
            dpi=dpi,
            start_page_id=0,
            end_page_id=None,
            image_type=ImageType.PIL,
        )
    except Exception as exc:
        logger.warning(f"Failed to render pages for rotation-corrected PDF: {exc}")
        return b""

    if not images_list:
        return b""

    for img_dict in images_list:
        try:
            image_rotate(img_dict)
        except Exception as exc:
            logger.warning(
                f"Failed to rotate page for rotation-corrected PDF: {exc}"
            )

    try:
        return pdf_images_to_pdf_bytes(images_list)
    except Exception as exc:
        logger.warning(
            f"Failed to generate rotation-corrected PDF bytes: {exc}"
        )
        return b""
