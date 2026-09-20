"""印章采集与 VLM 表格 OCR 补充编排。

文档级印章采集、页面标题提取、跨页表头归一化，以及
hybrid 后端阶段 A 的顶层入口 supplement_vlm_table_cells_with_ocr。"""

import os
import re
from bs4 import BeautifulSoup
from loguru import logger
from mineru.utils.custom.table_utils._common import (
    _cjk_count,
    _compact_norm,
)
from mineru.utils.custom.table_utils.detect import (
    _is_financial_statement_table,
    _is_invoice_table,
    _is_structurally_sparse_table,
)
from mineru.utils.custom.table_utils.ocr_guards import (
    _is_ghost_table,
)
from mineru.utils.custom.table_utils.ocr_fill import (
    supplement_empty_table_cells,
)


def _collect_doc_seals(pdf_info_list: list) -> set:
    """收集文档级印章文本（跨全部页面）用于对 OCR 池做语义源过滤。

    VLM 把印章/签章识别为 image 块内的 image span，其 content 为多行文本
    （如「中国工商银行股份有限公司 / 枣庄三八支行 / 业务专用章 / 编号」）。
    印章物理上存在于多数页面，但 VLM 并非每页都显式标出（如 p1 只有 table 块），
    故必须取**跨页超集**（与具体页面无关的固定印章词）。仅保留纯中文行
    （CJK≥4 且不含数字），排除签名戳（「张之祥20231018」）与打印日期戳
    （「打印日期20230506」）等含编号/日期的行，避免「日期/0.00」误伤。

    Args:
        pdf_info_list: 中间 JSON 的页面列表。

    Returns:
        印章规范化文本集合。
    """
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    seals = set()
    for page_info in pdf_info_list:
        for block in page_info.get("preproc_blocks", []):
            if block.get("type") != ContentType.IMAGE:
                continue
            for span in iter_block_spans(block):
                if span.get("type") != ContentType.IMAGE:
                    continue
                content = str(span.get("content", "") or "")
                for line in content.split("\n"):
                    line = line.strip()
                    if not line or line == "None":
                        continue
                    # 排除签名/日期戳：纯中文行（CJK≥4 且无数字）才是印章正文
                    if _cjk_count(line) >= 4 and not re.search(r"\d", line):
                        seals.add(_compact_norm(line))
    return seals


def _collect_page_title(page_info: dict) -> str:
    """收集页面标题（表格上方的 table_caption 或 CJK 居多的 text 块）。

    银行流水每页顶部有「中国工商银行对公客户账务明细」等标题；VLM 把标题
    放出为 table_caption 子块或表格上方的 text 块。「中」等页标题截断单字
    经 OCR 落入表格空列时，需比对本页标题判断（守卫 5A-3）。

    Args:
        page_info: 单页的中间 JSON。

    Returns:
        页标题规范化文本；无则为空字符串。
    """
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    table_bbox = None
    for block in page_info.get("preproc_blocks", []):
        if block.get("type") == ContentType.TABLE:
            table_bbox = block.get("bbox")
            break
    if not table_bbox or len(table_bbox) < 2:
        return ""

    title = ""
    for block in page_info.get("preproc_blocks", []):
        block_type = block.get("type")
        bbox = block.get("bbox") or []
        # 表格上方的 table_caption 子块优先
        if block_type == ContentType.TABLE:
            for sub in block.get("blocks", []):
                sub_bbox = sub.get("bbox") or []
                if (
                    sub.get("type") == "table_caption"
                    and len(sub_bbox) >= 2
                    and sub_bbox[1] < table_bbox[1] - 1
                ):
                    for span in iter_block_spans(sub):
                        title = _pick_title_span(span.get("content", ""))
                        if title:
                            return title
        # 表格上方的 text 块（bbox y0 在表格之上）
        elif (
            block_type == ContentType.TEXT
            and len(bbox) >= 2
            and bbox[1] < table_bbox[1] - 2
        ):
            for span in iter_block_spans(block):
                title = _pick_title_span(span.get("content", ""))
                if title:
                    return title
    return title


def _pick_title_span(content: object) -> str:
    """从 span 文本中提取 ≥10 字且 CJK 居多（≥1/2）的标题。

    Args:
        content: span 的 content（str 或含 "content" 键的 dict 列表）。

    Returns:
        规范化后的标题；不满足条件返回空字符串。
    """
    if isinstance(content, list):
        text = "".join(
            str(item.get("content", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    else:
        text = str(content or "")
    text = text.strip()
    if len(text) >= 10 and _cjk_count(text) >= len(text) // 2:
        return _compact_norm(text)
    return ""


def _normalize_header_text_across_pages(pdf_info_list: list) -> None:
    """跨页对齐表头文字：当某页表头以印章后缀结尾时，以纯文本基准归一化。

    规则（原则 9 跨页对照）：同一多页表格，若某页面某列表头文本以另一页面
    同列表头文本为真前缀且长度差 ≤4 → 截断为该前缀。典型场景：VLM 将印章
    叠印文字合并进表头（"转出金额专用章" → "转出金额"），非印章页的
    同列正确表头作为基准。

    Args:
        pdf_info_list: 中间 JSON 页面列表（本函数就地修改 span["html"]）。
    """
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    # col_idx → [(page_idx, table_idx, text, cell, soup, span, raw_html)]
    entries: dict[int, list] = {}

    for page_idx, page_info in enumerate(pdf_info_list):
        table_idx = 0
        for block in page_info.get("preproc_blocks", []):
            for span in iter_block_spans(block):
                if span.get("type") != ContentType.TABLE:
                    continue
                html = span.get("html", "")
                if not html:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                table = soup.find("table")
                if not table:
                    continue
                first_row = table.find("tr")
                if not first_row:
                    continue
                for col_idx, cell in enumerate(first_row.find_all(["td", "th"])):
                    text = cell.get_text().strip()
                    if text and len(text) >= 2:
                        entries.setdefault(col_idx, []).append(
                            (page_idx, table_idx, text, cell, soup, span, html)
                        )
                table_idx += 1

    for col_idx, col_entries in entries.items():
        if len(col_entries) < 2:
            continue
        # 找最短文本作为基准
        texts = [e[2] for e in col_entries]
        base = min(texts, key=len)
        if len(base) < 2:
            continue

        for page_idx, table_idx, text, cell, soup, span, raw_html in col_entries:
            if text == base:
                continue
            if not text.startswith(base):
                continue
            length_diff = len(text) - len(base)
            if length_diff <= 0 or length_diff > 4:
                continue  # 不是单纯后缀（或后缀太长）

            # 该表头是基准的超集（多了一个印章后缀），替换
            for child in list(cell.children):
                child.extract()
            cell.string = base
            # 序列化并更新 span["html"]
            _new_html = str(soup)
            if span.get("html", "") == raw_html:
                span["html"] = _new_html
            else:
                logger.warning(
                    f"守卫 11: span html changed since read (col{col_idx}), skip"
                )
            logger.debug(
                f"守卫 11 跨页表头归一化: 列{col_idx} "
                f"{text!r} -> {base!r} (长度差 {len(text)-len(base)})"
            )


def supplement_vlm_table_cells_with_ocr(
    pdf_info_list: list,
    hybrid_pipeline_model,
    image_writer=None,
) -> None:
    """使用 Pipeline OCR 识别结果补充 VLM 表格 HTML 中的空单元格。

    遍历所有表格 span，对有空单元格的表格：
    1. 加载表格截图
    2. 运行 PaddleOCR 获取文字识别结果
    3. 按行列聚类 OCR 文字为网格
    4. 将 OCR 网格文字填充到 VLM HTML 对应空单元格

    [自定义] 此函数由 hybrid_model_output_to_middle_json.py 中的 hook 调用。
    上游合并时此模块仅需保留，无需修改。

    Args:
        pdf_info_list: 中间 JSON 的页面列表。
        hybrid_pipeline_model: Hybrid pipeline 模型实例（含 ocr_model）。
        image_writer: 可选的 FileBasedDataWriter，用于解析图片相对路径。
            若提供且图片加载失败，会尝试从 image_writer 的根目录读取。
    """

    import cv2
    from bs4 import BeautifulSoup
    from mineru.backend.utils.para_block_utils import iter_block_spans
    from mineru.utils.enum_class import ContentType

    ocr_model = hybrid_pipeline_model.ocr_model
    filled_count = 0
    skipped_count = 0

    # 守卫 5：文档级印章文本（跨页超集，VLM 每页并不都标出印章 image 项）。
    # VLM 对同一 PDF 输出确定，印章词固定——一次性收集供全部页面比对。
    doc_seals = _collect_doc_seals(pdf_info_list)

    for page_info in pdf_info_list:
        # [自定义] 守卫 7-1：印章-表格重叠区域检测
        # 印章块（IMAGE type）与表格块（TABLE type）在页面坐标系中存在
        # bbox 重叠时，表格截图会包含印章图像，OCR 会把印章文字读入
        # 空单元格。检测重叠区域以供后续 OCR token 位置过滤。
        _table_bbox = None
        _seal_bboxes = []
        for _b in page_info.get("preproc_blocks", []):
            if _b.get("type") == ContentType.TABLE:
                _table_bbox = _b.get("bbox")
            if _b.get("type") == ContentType.IMAGE:
                _seal_bboxes.append(_b.get("bbox"))
        _seal_overlaps_page = []
        if _table_bbox and len(_table_bbox) >= 4:
            for _sb in _seal_bboxes:
                if _sb and len(_sb) >= 4:
                    _ox1 = max(_table_bbox[0], _sb[0])
                    _oy1 = max(_table_bbox[1], _sb[1])
                    _ox2 = min(_table_bbox[2], _sb[2])
                    _oy2 = min(_table_bbox[3], _sb[3])
                    if _ox1 < _ox2 and _oy1 < _oy2:
                        _seal_overlaps_page.append((_ox1, _oy1, _ox2, _oy2))

        # 守卫 5：本页标题（表格上方的 caption/text），供「单 CJK ∈ 页标题」判断
        page_title = _collect_page_title(page_info)
        chrome = {"seals": doc_seals, "title": page_title,
                  "seal_overlaps": _seal_overlaps_page,
                  "table_bbox": _table_bbox}
        for block in page_info.get("preproc_blocks", []):
            for span in iter_block_spans(block):
                if span.get("type") != ContentType.TABLE:
                    continue

                html = span.get("html", "")
                if not html:
                    continue

                # 检查是否有空单元格需要填充
                try:
                    soup = BeautifulSoup(html, "html.parser")
                    table = soup.find("table")
                    if not table:
                        continue
                    # 快速检查：是否存在空文本的 <td>
                    has_empty = any(
                        not cell.get_text().strip()
                        for cell in table.find_all("td")
                    )
                    # 检查是否存在含 <img> 的单元格
                    #（VLM 无法识别手写/签章文字时将其渲染为图片，
                    #   即使 get_text() 有标签文字，也需要 OCR 补充值文本）
                    has_img = any(
                        cell.find("img") is not None
                        for cell in table.find_all("td")
                    )
                    # 发票表格即使无空单元格也需 OCR，
                    # 用于检测和纠正 VLM 的多行拼接问题
                    is_invoice = _is_invoice_table(table)
                    if not has_empty and not has_img and not is_invoice:
                        continue
                    # 结构性稀疏表格（如财务报表、征信报告）的空单元格是合法留白，
                    # 非 VLM 遗漏，不应做 OCR 填充（否则会因列类型退化为全 "text" 而把
                    # 表头文字/行标签误填进空列，产生重复内容）。发票与含 <img> 的表格除外。
                    if _is_structurally_sparse_table(table) and not has_img and not is_invoice:
                        continue
                    # [自定义] 财务报表样式表格（表头含「行次」）即使非稀疏（密集报表，
                    # 大量「-」占位）其空单元格也是合法留白。OCR 行对齐错位会灌入
                    # 截断标签/合并数字/单字噪声，故跳过 OCR 补充。发票与含 <img> 表格除外。
                    if _is_financial_statement_table(table) and not has_img and not is_invoice:
                        continue
                except Exception:
                    continue

                # 获取表格图片路径并加载
                image_path = span.get("image_path", "")
                if not image_path:
                    skipped_count += 1
                    continue

                try:
                    table_img = cv2.imread(image_path)
                    if table_img is None:
                        # 图片路径可能为相对路径（如仅 hash 文件名），
                        # 尝试通过 image_writer 的根目录解析完整路径
                        if image_writer is not None and hasattr(image_writer, '_parent_dir'):
                            full_path = os.path.join(image_writer._parent_dir, image_path)
                            table_img = cv2.imread(full_path)
                    if table_img is None:
                        skipped_count += 1
                        continue
                except Exception:
                    skipped_count += 1
                    continue

                h, w = table_img.shape[:2]
                if h < 10 or w < 10:
                    skipped_count += 1
                    continue

                # 运行 PaddleOCR 获取文字
                try:
                    ocr_output = ocr_model.ocr(table_img, det=True, rec=True)
                except Exception:
                    logger.exception("表格图片 PaddleOCR 执行失败")
                    skipped_count += 1
                    continue

                if not ocr_output or not ocr_output[0]:
                    skipped_count += 1
                    continue

                ocr_results = ocr_output[0]

                # [自定义] 守卫 7-2：鬼影表格图像检测。
                # 部分 PDF 页面渲染产生的表格截图是鬼影/花屏图像，OCR 在其上
                # 读到固定位置重复的乱码数字碎片而非真实数据。检测到鬼影后
                # 跳过 OCR 补充，保留 VLM 原始结果（不加乱码，原则 1 不多不少）。
                if _is_ghost_table(ocr_results):
                    logger.debug(
                        f"守卫 7-2 鬼影表格跳过: {image_path.split('/')[-1]} "
                        f"({len(ocr_results)} tokens)"
                    )
                    skipped_count += 1
                    continue

                # 传递截图高度供守卫 7-1 坐标变换
                chrome["img_h"] = h

                # 调用表格补充函数
                try:
                    new_html = supplement_empty_table_cells(
                        html, ocr_results, w, chrome=chrome
                    )
                    if new_html != html:
                        span["html"] = new_html
                        filled_count += 1
                        logger.debug(
                            f"OCR 补充表格单元格成功："
                            f"图片={image_path.split('/')[-1]}"
                        )
                except Exception:
                    logger.exception("supplement_empty_table_cells 执行失败")
                    skipped_count += 1
                    continue

    if filled_count > 0:
        logger.info(
            f"Pipeline OCR 表格补充完成：填入了 {filled_count} 个表格的空单元格，"
            f"跳过 {skipped_count} 个表格"
        )

    # === [自定义] 守卫 11：跨页表头印章后缀剥离 ===
    # VLM 有时将印章叠印文字与表头合并（如 "转出金额专用章" 应为 "转出金额"），
    # 本步骤以跨页同列表头为基准做后缀剥离（仅改表头显示文本，不改列类型）。
    # 原则 9 跨页对照：同一表格跨多页时，非印章页的正确表头作为基准。
    _normalize_header_text_across_pages(pdf_info_list)


__all__ = [
    '_collect_doc_seals',
    '_collect_page_title',
    '_normalize_header_text_across_pages',
    '_pick_title_span',
    'supplement_vlm_table_cells_with_ocr',
]
