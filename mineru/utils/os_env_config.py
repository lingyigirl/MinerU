# Copyright (c) Opendatalab. All rights reserved.
import os


def get_op_num_threads(env_name: str) -> int:
    env_value = os.getenv(env_name, None)
    return get_value_from_string(env_value, -1)


def get_load_images_timeout() -> int:
    env_value = os.getenv('MINERU_PDF_RENDER_TIMEOUT', None)
    return get_value_from_string(env_value, 300)


def get_load_images_threads() -> int:
    env_value = os.getenv('MINERU_PDF_RENDER_THREADS', None)
    return get_value_from_string(env_value, 3)


def get_pdf_render_dpi() -> int:
    """获取 PDF 渲染 DPI（每英寸像素数）。

    环境变量 MINERU_PDF_RENDER_DPI 控制 PDF 页面渲染分辨率。
    默认 200 DPI，适用于大多数场景。对于低质量扫描件（如 CamScanner 压缩 PDF），
    可设置为 250 或 300 以提高 OCR 识别精度。

    Returns:
        PDF 渲染 DPI 值，默认 200。
    """
    env_value = os.getenv('MINERU_PDF_RENDER_DPI', None)
    dpi = get_value_from_string(env_value, 200)
    if dpi < 72:
        from loguru import logger
        logger.warning(
            f'MINERU_PDF_RENDER_DPI 值 {dpi} 过低（小于 72），使用默认值 200'
        )
        return 200
    return dpi


def get_value_from_string(env_value: str, default_value: int) -> int:
    if env_value is not None:
        try:
            num_threads = int(env_value)
            if num_threads > 0:
                return num_threads
        except ValueError:
            return default_value
    return default_value


if __name__ == '__main__':
    print(get_value_from_string('1', -1))
    print(get_value_from_string('0', -1))
    print(get_value_from_string('-1', -1))
    print(get_value_from_string('abc', -1))
    print(get_load_images_timeout())