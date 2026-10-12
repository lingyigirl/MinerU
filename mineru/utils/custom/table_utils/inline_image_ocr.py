"""VLM 表格内联图（``<img src="data:...">``）区域的手写字段 OCR 补回。

背景：VLM 把手写字段（户名/账号）判成 image block 后，mineru_vl_utils 的
``build_table_image_map`` 会将其吸收进表格 HTML（``<img src="data:...">``），
被吸收块的 bbox 随 ``post_process`` 丢弃，该区域因此在全部输出中没有任何文本
（实测定期存单户名「陈汉武」在 middle.json / content_list / md 中 grep 零命中）。

本模块在 data URI 被替换为落盘路径之前：

  1. 用模板匹配把 data URI 反查回页面原图得到真实 bbox（data URI 是页面
     ``crop((int(x1*W), int(y1*H), int(x2*W), int(y2*H)))`` 的整数像素精确子图，
     实测匹配分 0.9967~0.9980，本质是「精确裁剪的逆运算」）；
  2. 按真实 bbox 扩边（默认 10px）从页面重裁并 OCR（实测 200 DPI 下扩边
     6/10/16px 均可把垃圾识别恢复为 '陈汉武'/0.996，不扩边为 '随'/0.657，
     24px 起旁边印刷行被卷入、置信度回落）；
  3. 值通过 G1~G5 护栏后按「图片紧跟其后的标签」锚定规则写回标签之后
     （形如 ``户名：陈汉武``），使 content_list/md 出现可检索文本。

标签白名单目前只含「户名」：手写数字类字段（账号/客户号）的 OCR 结果跨扩边
不稳定、置信度与正确性无关（实测见 _LABEL_VALUE_PATTERNS 注释），
注入错误号码的危害大于字段缺失。

上游 hook：hybrid/vlm 后端 ``blocks_to_page_info`` 中 ``replace_inline_table_images``
之前调用 :func:`supplement_table_inline_images_with_ocr`。
"""

import base64
import re
from typing import Optional

import cv2
import numpy as np
from loguru import logger
from PIL import Image

from mineru.utils.custom.config import (
    get_inline_image_match_min_score,
    get_inline_image_ocr_enable,
    get_inline_image_ocr_pad_px,
    get_table_ocr_min_confidence,
)

# G1：标签白名单 + G2：值形状。
# 只收录「OCR 结果跨扩边稳定」的字段——实测 200 DPI 定期存单：
#   户名值（手写）在 pad 0/6/10/16/20 下均稳定为 '陈汉武'（0.995~0.997）；
#   账号值（手写）在同样扩边下每次结果都不同且位数 16/17/18 摇摆，置信度
#   0.82~0.93 与正确性完全无关 → 置信度闸门对此类字段无效。
# 注入一个错误的账号，危害大于该字段缺失，故账号/客户号不纳入白名单；
# 若后续要纳入，必须先引入「多扩边一致性复核」护栏（同字段两次识别需完全一致）。
# 户名形状收到 2~4 字：中文姓名主体长度，同时挡住印章文字（如「业务专用章」5 字）
# 误配到户名标签后的情形。
_LABEL_VALUE_PATTERNS = {
    "户名": re.compile(r"^[一-龥·]{2,4}$"),
}
_INLINE_IMG_RE = re.compile(r'<img\b[^>]*?src="(data:image/[^"]+)"[^>]*?>')
_MIN_TEMPLATE_SIDE = 8
_MIN_BBOX_OVERLAP = 0.5
# 单朝向匹配分达到该值即认定为精确子图，跳过其余朝向试探（实测 0.9967~0.9980）
_EXACT_MATCH_SCORE = 0.98

__all__ = ["supplement_table_inline_images_with_ocr"]


def supplement_table_inline_images_with_ocr(
    table_blocks: list,
    page_pil_img: Image.Image,
    scale: float,
    page_index: int = 0,
) -> int:
    """对表格 HTML 内联图做 OCR 并把识别值写回标签之后。

    Args:
        table_blocks: 中间 JSON 的 table 块列表（MagicModel.get_table_blocks()）。
        page_pil_img: 本页页面图（与送入 VLM 的图一致，含印章白化）。
        scale: 页面坐标到像素的缩放（image_dict["scale"]，200 DPI 时 ≈2.778）。
        page_index: 页序号（0 基），仅用于日志。

    Returns:
        发生注入的表格 span 数量。
    """
    if not get_inline_image_ocr_enable():
        return 0

    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    # data URI 由 VLM 输入页面图（白化后的 img_pil）裁出，故匹配也必须在该图上做
    page_bgr = np.asarray(page_pil_img.convert("RGB"))[:, :, ::-1].copy()
    injected_tables = 0
    for table_block in table_blocks or []:
        for span in iter_block_spans(table_block):
            if span.get("type") != ContentType.TABLE:
                continue
            html = span.get("html") or ""
            if "base64," not in html:
                continue
            new_html = _inject_values_into_html(
                html, span.get("bbox"), page_bgr, scale, page_index
            )
            if new_html != html:
                span["html"] = new_html
                injected_tables += 1

    if injected_tables:
        logger.info(
            f"表格内联图 OCR 补充完成：共 {injected_tables} 个表格补回了手写值文本"
        )
    return injected_tables


def _inject_values_into_html(
    html: str, table_bbox: Optional[list], page_bgr: np.ndarray, scale: float, page_index: int
) -> str:
    """扫描内联图 → 反查 bbox → 扩边重裁 → OCR → 护栏 → 在标签之后插入值。

    Args:
        html: 表格 span 的 HTML（此时 img 的 src 仍为 data URI）。
        table_bbox: 表格在页面坐标（72 DPI）下的 bbox，可为 None。
        page_bgr: 本页页面图的 BGR 数组。
        scale: 页面坐标到像素的缩放。
        page_index: 页序号（0 基），仅用于日志。

    Returns:
        注入后的 HTML；无任何注入时原样返回（保证幂等）。
    """
    parts, pos, hits = [], 0, 0
    for match in _INLINE_IMG_RE.finditer(html):
        label, label_end = _label_after(html, match.end())
        if label is None or not _no_existing_value(html, label_end):
            continue

        template = _decode_data_uri_bgr(match.group(1))
        if template is None:
            continue

        located = _match_template_on_page(page_bgr, template, table_bbox, scale)
        if located is None:
            logger.info(
                f"表格内联图 OCR——页={page_index} 标签={label} 跳过："
                f"模板匹配失败（可能非本页裁剪或分数不足）"
            )
            continue

        (x1, y1, x2, y2), score = located
        pad = get_inline_image_ocr_pad_px()
        crop = _padded_crop(page_bgr, (x1, y1, x2, y2), pad)
        text, conf = _ocr_single_line(crop)
        if not text:
            continue

        min_conf = get_table_ocr_min_confidence()
        if conf < min_conf:
            logger.info(
                f"表格内联图 OCR——页={page_index} 标签={label} 跳过："
                f"conf={conf:.3f} < 阈值={min_conf}"
            )
            continue

        if not _LABEL_VALUE_PATTERNS[label].match(text):
            logger.info(
                f"表格内联图 OCR——页={page_index} 标签={label} 跳过："
                f"值形状不符（text={text}）"
            )
            continue

        parts.append(html[pos:label_end])
        parts.append(f"：{text}")
        pos = label_end
        hits += 1
        logger.info(
            f"表格内联图 OCR 补充——页={page_index} 标签={label} 值={text} "
            f"conf={conf:.3f} 匹配分={score:.3f} bbox=({x1},{y1},{x2},{y2}) 扩边={pad}px"
        )

    if not hits:
        return html
    parts.append(html[pos:])
    return "".join(parts)


def _label_after(html: str, pos: int) -> tuple[Optional[str], int]:
    """G1：取 ``<img>`` 之后紧跟的白名单标签。

    Args:
        html: 表格 HTML。
        pos: ``<img>`` 标签结束位置。

    Returns:
        (标签, 标签结束位置)；未命中白名单时返回 (None, pos)。
    """
    for label in _LABEL_VALUE_PATTERNS:
        if html.startswith(label, pos):
            return label, pos + len(label)
    return None, pos


def _no_existing_value(html: str, pos: int) -> bool:
    """G3：标签之后到下一个标签之间不得有既有正文（保证幂等、不覆盖）。

    Args:
        html: 表格 HTML。
        pos: 标签结束位置。

    Returns:
        True 表示该位置可以安全注入值。
    """
    next_tag = html.find("<", pos)
    tail = html[pos:] if next_tag < 0 else html[pos:next_tag]
    return not tail.strip()


def _decode_data_uri_bgr(data_uri: str) -> Optional[np.ndarray]:
    """解码 data URI 为 BGR 模板图，过小或解码失败返回 None。

    Args:
        data_uri: 形如 ``data:image/jpeg;base64,...`` 的数据串。

    Returns:
        BGR 模板图；不可用时 None。
    """
    try:
        raw = base64.b64decode(data_uri.split("base64,", 1)[1])
        bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        logger.exception("表格内联图 OCR——data URI 解码失败")
        return None
    if bgr is None or min(bgr.shape[:2]) < _MIN_TEMPLATE_SIDE:
        return None
    return bgr


def _match_template_on_page(
    page_bgr: np.ndarray, template_bgr: np.ndarray, table_bbox: Optional[list], scale: float
) -> Optional[tuple[tuple[int, int, int, int], float]]:
    """G5：在页面图上定位模板，返回 (bbox_px, 匹配分)；失败返回 None。

    表块带角度时 mineru_vl_utils 已旋转过裁剪图，故做 4 朝向兜底；无旋转
    朝向命中精确匹配分（≥0.98）即提前结束，避免多余的全图匹配开销。

    Args:
        page_bgr: 页面图 BGR 数组。
        template_bgr: data URI 解码出的模板图。
        table_bbox: 表格页面坐标 bbox，可为 None（None 时跳过几何护栏）。
        scale: 页面坐标到像素的缩放。

    Returns:
        ((x1, y1, x2, y2), score) 或 None。
    """
    page_h, page_w = page_bgr.shape[:2]
    threshold = get_inline_image_match_min_score()
    best: Optional[tuple[tuple[int, int, int, int], float]] = None

    for angle in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        candidate = template_bgr if angle is None else cv2.rotate(template_bgr, angle)
        tpl_h, tpl_w = candidate.shape[:2]
        if tpl_h >= page_h or tpl_w >= page_w:  # matchTemplate 要求模板不大于图像
            continue
        result = cv2.matchTemplate(page_bgr, candidate, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if best is None or max_val > best[1]:
            best = ((max_loc[0], max_loc[1], max_loc[0] + tpl_w, max_loc[1] + tpl_h), float(max_val))
        if best[1] >= _EXACT_MATCH_SCORE:
            break

    if best is None or best[1] < threshold:
        return None
    if not _overlaps_table(best[0], table_bbox, scale):
        return None
    return best


def _overlaps_table(bbox_px: tuple[int, int, int, int], table_bbox: Optional[list], scale: float) -> bool:
    """G5：匹配框与表格 bbox（×scale 换算到像素）的面积重叠是否达阈值。

    Args:
        bbox_px: 模板匹配得到的像素框。
        table_bbox: 表格页面坐标 bbox，可为 None。
        scale: 页面坐标到像素的缩放。

    Returns:
        True 表示重叠达标（或无法判定时放行）。
    """
    if not table_bbox or not scale:
        return True
    x1, y1, x2, y2 = bbox_px
    tx1, ty1, tx2, ty2 = (float(v) * scale for v in table_bbox)
    inter_w = max(0.0, min(x2, tx2) - max(x1, tx1))
    inter_h = max(0.0, min(y2, ty2) - max(y1, ty1))
    area = max(1.0, float((x2 - x1) * (y2 - y1)))
    return (inter_w * inter_h) / area >= _MIN_BBOX_OVERLAP


def _padded_crop(page_bgr: np.ndarray, bbox_px: tuple[int, int, int, int], pad: int) -> np.ndarray:
    """按 bbox 扩边并从页面重裁（复现 0.99 识别率的关键步骤）。

    被 VLM 紧框切掉的笔画只存在于页面原图上，故必须回页面重裁而非对
    crop 补白底；扩边同时给 rec 提供完整行上下文。

    Args:
        page_bgr: 页面图 BGR 数组。
        bbox_px: 模板匹配得到的像素框。
        pad: 扩边像素。

    Returns:
        扩边后的裁剪图。
    """
    page_h, page_w = page_bgr.shape[:2]
    x1, y1, x2, y2 = bbox_px
    return page_bgr[
        max(0, y1 - pad):min(page_h, y2 + pad),
        max(0, x1 - pad):min(page_w, x2 + pad),
    ]


def _ocr_single_line(crop_bgr: np.ndarray) -> tuple[str, float]:
    """单行 rec（det=False），复用 custom 包的 PaddleOCR 单例。

    Args:
        crop_bgr: 待识别裁剪图（BGR）。

    Returns:
        (文本, 置信度)；失败返回 ("", 0.0)。
    """
    try:
        from mineru.utils.custom.doc_classifier import get_ocr

        result = get_ocr().ocr(crop_bgr, det=False, rec=True)
        if not result or not result[0]:
            return "", 0.0
        text, score = result[0][0]
        return str(text), float(score)
    except Exception:
        logger.exception("表格内联图 OCR 执行失败")
        return "", 0.0
