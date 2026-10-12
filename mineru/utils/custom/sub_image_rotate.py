"""图块级朝向旋转的置信度门控。

上游/fork 的 `_rotate_sub_image` 直接使用朝向分类器 `predict_direct` 的 argmax
结果旋转图块。该模型对小尺寸/宽扁图块（手写值、名章、金额行）判别力极低——
实测四类概率全部落在 0.185~0.443 的噪声带内（均匀基线 0.25），argmax 即噪声：
200 DPI 定期存单 8 张落盘图错转 4 张（户名值/金额大写/名章转 180°，印章小转 270°）。

本模块对非 "0" 标签增加置信度门槛，旋转与跳过均打 INFO 日志（含来源、尺寸、
label、conf、阈值），使问题可像页面级（custom/pdf_utils.py 的「旋转修正PDF——」）
一样从日志直接定位。阈值与开关见 mineru/utils/custom/config.py。
"""

from typing import Any

from loguru import logger
from PIL import Image

from mineru.utils.custom.config import (
    get_sub_image_rotate_enable,
    get_sub_image_rotate_min_conf,
)
from mineru.utils.custom.pdf_utils import get_classified_rotation


def rotate_sub_image_if_confident(
    crop_img: Image.Image,
    cls_model: Any,
    source: str = "",
) -> Image.Image:
    """图块朝向检测，置信度达标才旋转，否则保留原图。

    Args:
        crop_img: 待检测的图块。
        cls_model: 朝向分类模型（PaddleOrientationClsModel 或其鸭子类型替身）。
        source: 来源标识（落盘文件名或 "表格内联图:page=N"），用于日志定位。

    Returns:
        旋转后的图块；label=="0"、低置信、开关关闭时原样返回。
    """
    if not get_sub_image_rotate_enable():
        return crop_img

    label, conf = get_classified_rotation(cls_model, crop_img)
    if label == "0":
        # 正常路径静默（与页面级一致：label==0 不打 INFO，避免日志刷屏）
        return crop_img

    theta = get_sub_image_rotate_min_conf()
    width, height = crop_img.size
    if conf >= theta:
        rotated = cls_model.rotate_pil_image(crop_img, label)
        logger.info(
            f"图块旋转修正——执行旋转：来源={source}，尺寸={width}x{height}，"
            f"label={label}，conf={conf:.3f} >= 阈值={theta}"
        )
        return rotated

    logger.info(
        f"图块旋转修正——跳过旋转（低置信）：来源={source}，尺寸={width}x{height}，"
        f"label={label}，conf={conf:.3f} < 阈值={theta}，保留原图"
    )
    return crop_img


__all__ = ["rotate_sub_image_if_confident"]
