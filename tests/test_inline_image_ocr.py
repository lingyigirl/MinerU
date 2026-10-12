"""表格内联图 OCR 补回的隔离验证（合成页面图 + monkeypatch OCR，不加载真实模型）。

覆盖：模板匹配反查 + 页面扩边重裁（断言 OCR 收到的裁剪尺寸）+ 五道护栏
（标签白名单/值形状/无既有值/置信度/几何）+ 开关与幂等。
"""
import base64

import cv2
import numpy as np
import pytest
from PIL import Image

import mineru.utils.custom.doc_classifier as dc
import mineru.utils.custom.table_utils.inline_image_ocr as iio
from mineru.utils.enum_class import ContentType

PAGE_W, PAGE_H = 600, 400
PATCH_X, PATCH_Y, PATCH_W, PATCH_H = 300, 300, 100, 40


class FakeOcr:
    """记录每次 rec 收到的裁剪图尺寸，返回预置结果。"""

    def __init__(self, text="陈汉武", score=0.996):
        self.text = text
        self.score = score
        self.shapes = []

    def ocr(self, img, det=True, rec=True, **kwargs):
        self.shapes.append(np.asarray(img).shape)
        return [[(self.text, self.score)]]


def _page_with_patch():
    """平滑渐变页面（PIL RGB）+ 一块高结构纹理图，构成无歧义的匹配峰。"""
    page = np.zeros((PAGE_H, PAGE_W, 3), np.uint8)
    page[:, :, 1] = np.linspace(0, 255, PAGE_W, dtype=np.uint8)[None, :]
    patch = np.full((PATCH_H, PATCH_W, 3), 40, np.uint8)
    cv2.rectangle(patch, (2, 2), (PATCH_W - 3, PATCH_H - 3), (250, 250, 250), 2)
    cv2.line(patch, (10, 20), (PATCH_W - 10, 20), (250, 250, 250), 4)
    cv2.circle(patch, (30, 28), 8, (250, 250, 250), -1)
    page[PATCH_Y:PATCH_Y + PATCH_H, PATCH_X:PATCH_X + PATCH_W] = patch
    return Image.fromarray(page[:, :, ::-1]), patch


def _data_uri(patch):
    ok, buf = cv2.imencode(".jpg", patch)
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _blocks(html):
    return [{
        "type": ContentType.TABLE,
        "blocks": [{
            "type": "table_body",
            "lines": [{"spans": [{
                "type": ContentType.TABLE,
                "html": html,
                "bbox": [PATCH_X - 20, PATCH_Y - 20, PATCH_X + PATCH_W + 20, PATCH_Y + PATCH_H + 20],
            }]}],
        }],
    }]


def _img_html(label="户名", tail=""):
    page, patch = _page_with_patch()
    html = f'<img src="{_data_uri(patch)}"/>{label}{tail}'
    return page, html


def _run(monkeypatch, html, page, text="陈汉武", score=0.996):
    fake = FakeOcr(text=text, score=score)
    monkeypatch.setattr(dc, "get_ocr", lambda: fake)
    monkeypatch.setenv("MINERU_INLINE_IMAGE_OCR_PAD_PX", "10")
    blocks = _blocks(html)
    count = iio.supplement_table_inline_images_with_ocr(blocks, page, 1.0, page_index=0)
    return count, blocks[0]["blocks"][0]["lines"][0]["spans"][0]["html"], fake


def test_inject_name_value(monkeypatch):
    """正常路径：注入 `户名：陈汉武`，且 OCR 收到的是扩边后从页面重裁的图。"""
    page, html = _img_html()
    count, new_html, fake = _run(monkeypatch, html, page)
    assert count == 1
    assert "户名：陈汉武" in new_html
    assert "<img" in new_html, "原图块不得被删除"
    assert fake.shapes == [(PATCH_H + 20, PATCH_W + 20, 3)], "必须按 bbox 扩边 10px 从页面重裁"


def test_low_conf_no_inject(monkeypatch):
    """识别置信度不足 → 不改 HTML（本页账号/大写金额落此档）。"""
    page, html = _img_html()
    count, new_html, _ = _run(monkeypatch, html, page, score=0.355)
    assert count == 0
    assert new_html == html


def test_shape_guard_rejects_seal_text(monkeypatch):
    """印章文字即使落在「户名」标签后且高置信，也会被值形状护栏拒绝。"""
    page, html = _img_html(label="户名")
    count, new_html, _ = _run(monkeypatch, html, page, text="业务专用章", score=0.99)
    assert count == 0
    assert new_html == html


def test_handwritten_account_blocked(monkeypatch):
    """手写账号不纳入白名单：实测其 OCR 跨扩边不稳定（置信度不可信），
    注入错误号码的危害大于字段缺失，故即便高置信也不注入。"""
    page, html = _img_html(label="账号")
    count, new_html, _ = _run(monkeypatch, html, page, text="3631101223945373", score=0.93)
    assert count == 0
    assert new_html == html


def test_no_value_after_label_skips(monkeypatch):
    """标签后已有正文（如 VLM 已给出 `户名：张三`）→ 不注入、不覆盖。"""
    page, html = _img_html(tail="：张三")
    count, new_html, _ = _run(monkeypatch, html, page)
    assert count == 0
    assert new_html == html


def test_non_whitelisted_label_skips(monkeypatch):
    """非白名单标签（如印章图后面跟的「存入金额」）→ 不注入。"""
    page, html = _img_html(label="存入金额(大写)")
    count, new_html, _ = _run(monkeypatch, html, page)
    assert count == 0
    assert new_html == html


def test_match_fail_no_inject(monkeypatch):
    """模板不在页面上（噪声图）→ 匹配分不足，不注入。"""
    page, _ = _page_with_patch()
    noise = np.random.RandomState(0).randint(0, 255, (40, 100, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", noise)
    assert ok
    html = f'<img src="data:image/jpeg;base64,{base64.b64encode(buf.tobytes()).decode()}"/>户名'
    count, new_html, _ = _run(monkeypatch, html, page)
    assert count == 0
    assert new_html == html


def test_switch_off(monkeypatch):
    """总开关关闭 → 直接返回 0，不扫描。"""
    page, html = _img_html()
    monkeypatch.setenv("MINERU_INLINE_IMAGE_OCR", "false")
    blocks = _blocks(html)
    assert iio.supplement_table_inline_images_with_ocr(blocks, page, 1.0, page_index=0) == 0
    assert blocks[0]["blocks"][0]["lines"][0]["spans"][0]["html"] == html


def test_idempotent(monkeypatch):
    """重复执行只注入一次（标签后已有值 → 第二遍跳过）。"""
    page, html = _img_html()
    _, first, _ = _run(monkeypatch, html, page)
    count, second, _ = _run(monkeypatch, first, page)
    assert count == 0
    assert second == first


def test_non_table_span_untouched(monkeypatch):
    """非表格 span（无 table 类型/无 data URI）不参与。"""
    page, _ = _page_with_patch()
    blocks = [{"type": "text", "lines": [{"spans": [{"type": "text", "content": "户名"}]}]}]
    monkeypatch.setattr(dc, "get_ocr", lambda: FakeOcr())
    assert iio.supplement_table_inline_images_with_ocr(blocks, page, 1.0, page_index=0) == 0


@pytest.mark.parametrize("bad_table_bbox", [None, [0, 0, 10, 10]])
def test_geometry_guard_blocks_far_match(monkeypatch, bad_table_bbox):
    """匹配到的位置与表格 bbox 无重叠 → 判为非本表裁剪，不注入。"""
    page, html = _img_html()
    monkeypatch.setattr(dc, "get_ocr", lambda: FakeOcr())
    monkeypatch.setenv("MINERU_INLINE_IMAGE_OCR_PAD_PX", "10")
    blocks = _blocks(html)
    blocks[0]["blocks"][0]["lines"][0]["spans"][0]["bbox"] = bad_table_bbox
    count = iio.supplement_table_inline_images_with_ocr(blocks, page, 1.0, page_index=0)
    if bad_table_bbox is None:
        assert count == 1, "无表格 bbox 时几何护栏放行（无法判定）"
    else:
        assert count == 0, "匹配位置远离表格时不得注入"
