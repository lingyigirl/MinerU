"""自定义 PDF 处理工具。

此模块封装团队对 MinerU PDF 处理的扩展功能，
与上游 pdf_image_tools.py 解耦，便于合并上游更新。
"""

from mineru.utils.custom.config import (
    get_rotate_geom_enable,
    get_rotate_geom_majority_min,
)
from mineru.utils.enum_class import ImageType
from mineru.utils.pdf_image_tools import (
    DEFAULT_PDF_IMAGE_DPI,
    load_images_from_pdf_core,
)
import numpy as np


def _images_to_pdf_bytes_with_dpi(images_list: list, dpi: int) -> bytes:
    """将修正后的 PIL 图像合成为带 DPI 元数据的 PDF。

    PIL 默认 save(format="PDF") 时按 1px=1pt（72 DPI）处理，导致 dpi 渲染的
    高清图存出的 PDF 页面尺寸被放大（像素数 = 页尺寸，源页面尺寸被破坏）。
    通过传入 resolution=dpi 写入正确的 DPI 元数据，使页面尺寸 =
    像素 / dpi * 72，与源页面一致，同时图像像素保持 dpi 分辨率不降采样。

    Args:
        images_list: 形如 [{"img_pil": PIL.Image}, ...] 的修正后图像列表。
        dpi: 渲染 DPI，用于写入 PDF 元数据（页面尺寸据此换算）。

    Returns:
        合成的 PDF 字节串；列表为空时返回 b""。
    """
    from io import BytesIO

    pil_images = [img_dict["img_pil"].convert("RGB") for img_dict in images_list]
    if not pil_images:
        return b""

    pdf_bytes_io = BytesIO()
    pil_images[0].save(
        pdf_bytes_io,
        format="PDF",
        save_all=True,
        append_images=pil_images[1:],
        resolution=float(dpi),
    )
    return pdf_bytes_io.getvalue()


def _get_classified_rotation(cls_model, pil_img) -> tuple[str, float]:
    """单页朝向分类，返回 (label, softmax置信度)。

    比 predict_direct 多返回置信度（argmax 概率），供两级判定的阈值否决权使用。
    """
    # 保证 RGB 3 通道（RGBA/灰度统一转 RGB），与 predict_direct 的预处理一致
    np_img = np.asarray(pil_img.convert("RGB"))
    x = cls_model.preprocess(np_img)
    (result,) = cls_model.sess.run(None, {"x": x})
    logits = result[0].flatten().astype(np.float64)
    # softmax（数值稳定）
    logits -= logits.max()
    exp_logits = np.exp(logits)
    probs = exp_logits / exp_logits.sum()
    label = cls_model.labels[int(np.argmax(probs))]
    conf = float(probs.max())
    return label, conf


def generate_rotation_corrected_pdf(
    pdf_bytes: bytes,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,
    theta: float = 0.45,
) -> bytes:
    """将 PDF 所有页面渲染并做朝向检测+旋转修正，合成为一个方向正确的 PDF。

    采用两级判定（文档多数派先验 + 置信度否决权），解决低信息量页面
    （如整页表格、目录）上单页分类器置信度接近随机、argmax 误判 180°
    导致正页被翻转的问题：

    1. 逐页用 ONNX 朝向分类器得到 argmax 方向 label 和置信度 conf
    2. 统计文档级多数派方向 majority（绝大多数页方向一致）
    2.5 [自定义] 统计文档级几何多数派（页面宽高比）：真旋转页的 conf
       （0.435-0.448）与误判页（≤0.439）分布重叠，单靠 theta 无法分离；
       而宽高比是与置信度正交的免费证据——银行流水等表格文档以横向页为
       绝对多数，异向页（纵向）与真旋转页集合重合。几何只决定"是否豁免
       阈值"，方向仍由分类器 label 决定。
    3. 两阶段判定：
       - label == majority：跟随多数派，按 label 旋转（无需置信度，多数派即文档整体方向）
       - label != majority：仅当 conf >= theta 或该页为几何异常页才执行旋转
         （高置信独立判定，或几何证据豁免阈值，处理文档内个别页真旋转的
         场景）；否则判定为低置信偏离多数派的误判，跳过旋转、保留原页
    4. 将修正后的所有页面合成单 PDF，写入 resolution=dpi 元数据，
       使页面尺寸与源 PDF 一致，同时图像像素保持 dpi 分辨率不降采样
    5. [不再负责] 红色印章去除已移入 hybrid/vlm 的图像级处理（option 2），
       在加载 PDF 页面图像后统一白化，不修改 pdf_bytes，从而保留文本层。
       参见 hybrid_analyze.py / vlm_analyze.py 的 [自定义] 图像级白化钩子。
    6. [自定义] 若所有页面都无需旋转，直接返回空字节，由调用方沿用原始
       pdf_bytes——避免无谓的全页光栅化销毁原生文本层。

    注意：此函数会用 pypdfium2 打开文档，调用者需确保 pdfium_guard 锁可用。

    Returns:
        修正后的 PDF 字节流；若无页面需要旋转则返回 b""（调用方应沿用原始字节）。

    Args:
        pdf_bytes: 原始 PDF 字节流。
        dpi: 渲染 DPI，默认取环境配置（200）。
        theta: 置信度阈值（默认 0.45）。偏离多数派的页面只有 conf >= theta
            时才执行独立旋转，否则视为误判跳过。θ 选取依据：本模型对
            非自然场景页面（空白+细线为主）四类置信度均 ~0.41-0.45，
            误判页 ~0.44，与正页/真旋转页区间重叠，故 theta 不宜过低，
            0.45 是能拦截误判页（0.439 < 0.45）的保守取值。判断负担由
            "单页阈值"转移到"偏离多数派的否决权"，对纯正页文档零误转。
            实测真旋转页 conf=0.4354 < 0.45，故 theta 单独无法同时"拦截
            误判页"与"采纳真旋转页"，需由几何判据（步骤 2.5）兜住后者。
    """
    from collections import Counter
    from loguru import logger
    from mineru.model.ori_cls.paddle_ori_cls import PaddleOrientationClsModel

    try:
        images_list = load_images_from_pdf_core(
            pdf_bytes,
            dpi=dpi,
            start_page_id=0,
            end_page_id=None,
            image_type=ImageType.PIL,
        )
    except Exception as exc:
        logger.warning(
            f"旋转修正PDF——页面渲染失败（pdf_bytes长度={len(pdf_bytes)}字节，dpi={dpi}）: {exc}"
        )
        return b""

    if not images_list:
        logger.warning(
            f"旋转修正PDF——渲染结果为空（pdf_bytes长度={len(pdf_bytes)}字节，dpi={dpi}），"
            f"可能原因：PDF页数为0、渲染DPI下无有效页面、或PDF格式不被pypdfium2支持"
        )
        return b""

    cls_model = PaddleOrientationClsModel(ocr_engine=None)

    # Phase 1: 逐页分类，收集 (label, conf)
    page_votes = []
    for idx, img_dict in enumerate(images_list):
        try:
            label, conf = _get_classified_rotation(cls_model, img_dict["img_pil"])
            page_votes.append((label, conf))
        except Exception as exc:
            logger.warning(f"旋转修正PDF——第{idx + 1}页分类失败: {exc}")
            page_votes.append(("0", 0.0))  # 分类失败默认不旋转

    # Phase 2: 文档级多数派方向
    majority = Counter(label for label, _ in page_votes).most_common(1)[0][0]
    logger.info(
        f"旋转修正PDF——文档多数派方向={majority}，"
        f"逐页判定={[(i + 1, page_votes[i][0], round(page_votes[i][1], 3)) for i in range(len(page_votes))]}"
    )

    # Phase 1.5: 文档级几何多数派（页面宽高比）—— 与置信度正交的第二判据。
    # 真旋转页的置信度（实测 0.4354）与误判页上限（0.439）分布重叠，theta
    # 单参数无法同时"拦截误判页"与"采纳真旋转页"（调低是零和）。宽高比是
    # 免费的独立证据：渲染尺寸遵循 MediaBox（含 /Rotate 生效后的方向），
    # 无需额外调用 pypdfium2 API；表格类文档以横向页为绝对多数，异向页
    # 与真旋转页集合重合（实测 96 页：93 横 3 纵，3 纵页即 3 真旋转页，
    # 零误差零误伤）。几何只决定"是否豁免阈值"，方向仍由分类器 label 决定。
    aspects = [
        "landscape"
        if img_dict["img_pil"].size[0] > img_dict["img_pil"].size[1]
        else "portrait"
        for img_dict in images_list
    ]
    geom_majority = Counter(aspects).most_common(1)[0][0]
    geom_majority_ratio = aspects.count(geom_majority) / len(aspects)
    geom_decisive = (
        get_rotate_geom_enable()
        and geom_majority_ratio >= get_rotate_geom_majority_min()
    )
    # 多数派占比不足（纯竖版扫描件混排等）时判据自动失效，退化为纯阈值判定
    geom_outlier_set = (
        {i for i, a in enumerate(aspects) if a != geom_majority}
        if geom_decisive
        else set()
    )
    logger.info(
        f"旋转修正PDF——文档几何多数派={geom_majority}"
        f"（占比 {geom_majority_ratio:.2f}，判据{'生效' if geom_decisive else '不生效'}），"
        f"几何异常页={sorted(i + 1 for i in geom_outlier_set)}"
    )

    # Phase 3: 两级判定 → 旋转修正
    skip_count = 0
    rotated_count = 0
    for idx, (img_dict, (label, conf)) in enumerate(zip(images_list, page_votes)):
        try:
            if label == majority:
                # 跟随多数派：文档内绝大多数页方向一致，即使某页置信度低也跟随
                if label != "0":
                    img_dict["img_pil"] = cls_model.rotate_pil_image(
                        img_dict["img_pil"], label
                    )
                    rotated_count += 1
            else:
                # 偏离多数派：两种情形可执行独立旋转——
                # (1) 置信度达标（原有逻辑）；
                # (2) 几何异常页（宽高比偏离文档多数派）→ 豁免阈值，见 Phase 1.5。
                # 注意几何只豁免"是否旋转"，方向仍由 label 决定；label == "0"
                # （分类器认为方向正确）时即便几何异常也不旋转，避免凭几何翻转正页。
                if (conf >= theta or idx in geom_outlier_set) and label != "0":
                    img_dict["img_pil"] = cls_model.rotate_pil_image(
                        img_dict["img_pil"], label
                    )
                    rotated_count += 1
                    if idx in geom_outlier_set and conf < theta:
                        logger.info(
                            f"旋转修正PDF——第{idx + 1}页按几何判据旋转"
                            f"（label={label}, conf={conf:.3f} < theta={theta}，"
                            f"该页 {aspects[idx]} 偏离文档多数派 {geom_majority}）"
                        )
                else:
                    skip_count += 1
                    logger.info(
                        f"旋转修正PDF——第{idx + 1}页跳过旋转（label={label}, "
                        f"conf={conf:.3f} < theta={theta}，偏离多数派={majority}，"
                        f"视为低置信误判，保留原页）"
                    )
        except Exception as exc:
            logger.warning(
                f"旋转修正PDF——第{idx + 1}页旋转失败: {exc}"
            )

    if skip_count > 0:
        logger.info(
            f"旋转修正PDF——共跳过 {skip_count}/{len(images_list)} 页"
            f"（低置信偏离多数派）"
        )

    # [自定义] 无任何页面需要旋转 → 不重编码 PDF，返回空字节让调用方沿用原始
    # pdf_bytes。重编码会把整页光栅化，销毁原生文本层，使下游 txt_spans_extract
    # 拿不到字符而降级为全文 OCR（精度与效率双降）。此处早退同时跳过印章白化，
    # 属于已知取舍：需要白化的文档靠文本层读字 + span_gap_rescue 兜底。
    if rotated_count == 0:
        if geom_outlier_set:
            # 「判定需要旋转但未被采纳」≠「无需旋转」：两者都返回 b""，但必须
            # 让日志可区分。历史排查成本正来自此处混淆——真旋转页被置信度阈值
            # 否决后，日志只显示"N 页均无需旋转"，把"判据失灵"伪装成"常态"。
            logger.warning(
                f"旋转修正PDF——{len(images_list)}页均未旋转，但存在几何异常页 "
                f"{sorted(i + 1 for i in geom_outlier_set)} 未被采纳"
                f"（分类器 label 与几何证据冲突或为 0），保留原始PDF（含文本层）"
            )
        else:
            logger.info(
                f"旋转修正PDF——{len(images_list)}页均无需旋转，保留原始PDF（含文本层）"
            )
        return b""

    # 继续合成 PDF：有页面经过了旋转修正，必须重新编码（旋转页的光栅化
    # 结果不含文本层，属已知取舍）。印章白化已移入 hybrid_analyze.py 的图像级
    # 处理（option 2 ②）：加载旋转后 PDF 的图像时统一白化，此处不再操作。
    try:
        return _images_to_pdf_bytes_with_dpi(images_list, dpi)
    except Exception as exc:
        logger.warning(
            f"旋转修正PDF——图片合成PDF失败（{len(images_list)}张修正后图片）: {exc}"
        )
        return b""
