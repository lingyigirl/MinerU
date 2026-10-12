"""图块级旋转置信度门控的隔离验证（monkeypatch 掉分类器，不加载 ONNX/OCR 模型）。

背景：_rotate_sub_image 原实现直接取朝向分类器 argmax 旋转图块，对这类小图
无判别力（四类概率落在 0.185~0.443 噪声带内），实测 8 张图错转 4 张。门控要求
非 "0" 标签必须 conf >= 阈值（默认 0.45）才旋转，并打出可定位的 INFO 日志。
"""
import sys
from contextlib import contextmanager

import numpy as np
from loguru import logger
from PIL import Image

import mineru.utils.custom.sub_image_rotate as s
import mineru.utils.pdf_image_tools as pit


class FakeCls:
    """labels 顺序与 PaddleOrientationClsModel 一致。"""

    labels = ["0", "90", "180", "270"]

    def rotate_pil_image(self, img, label):
        if label == "270":
            return img.rotate(-90, expand=True)
        if label == "90":
            return img.rotate(90, expand=True)
        if label == "180":
            return img.rotate(180, expand=True)
        return img


def _img(width=100, height=60):
    """构造一块有纹理的图，保证旋转后逐像素可区分。"""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = 200
    arr[height // 3: 2 * height // 3, width // 3: 2 * width // 3] = 255
    return Image.fromarray(arr)


@contextmanager
def capture_logs(level="INFO"):
    """收集 loguru 日志文本，避免依赖 pytest-loguru。"""
    records = []
    sink_id = logger.add(lambda msg: records.append(str(msg)), level=level, format="{message}")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _patch_vote(monkeypatch, label, conf):
    monkeypatch.setattr(s, "get_classified_rotation", lambda cls, img: (label, conf))


def test_low_conf_180_skipped(monkeypatch):
    """本缺陷现场：户名值 135x89 被判 180/conf=0.403 → 保留原图 + INFO 日志。"""
    _patch_vote(monkeypatch, "180", 0.403)
    img = _img()
    with capture_logs() as records:
        out = s.rotate_sub_image_if_confident(img, FakeCls(), source="1e66d094.jpg")
    assert out is img, "低置信不得翻转图块"
    assert any("跳过旋转（低置信）" in r for r in records)
    assert any("conf=0.403" in r and "阈值=0.45" in r and "1e66d094.jpg" in r for r in records)


def test_high_conf_180_rotated(monkeypatch):
    """真旋转图块（高置信）仍要旋转，门控不能把功能关死。"""
    _patch_vote(monkeypatch, "180", 0.95)
    img = _img()
    with capture_logs() as records:
        out = s.rotate_sub_image_if_confident(img, FakeCls(), source="x.jpg")
    assert out.tobytes() == img.rotate(180, expand=True).tobytes()
    assert any("执行旋转" in r and "conf=0.950" in r for r in records)


def test_label_zero_silent(monkeypatch):
    """label=='0' 是正常路径，不打日志（避免刷屏）。"""
    _patch_vote(monkeypatch, "0", 0.44)
    img = _img()
    with capture_logs() as records:
        out = s.rotate_sub_image_if_confident(img, FakeCls(), source="y.jpg")
    assert out is img
    assert not any("图块旋转修正" in r for r in records)


def test_switch_off(monkeypatch):
    """总开关关闭 → 高置信也不旋转。"""
    monkeypatch.setenv("MINERU_SUB_IMAGE_ROTATE", "false")
    _patch_vote(monkeypatch, "270", 0.99)
    img = _img()
    assert s.rotate_sub_image_if_confident(img, FakeCls(), source="z.jpg") is img


def test_theta_zero_restores_legacy(monkeypatch):
    """阈值置 0 → 恢复旧行为（argmax 即旋转），保证可一键回退。"""
    monkeypatch.setenv("MINERU_SUB_IMAGE_ROTATE_MIN_CONF", "0")
    _patch_vote(monkeypatch, "180", 0.30)
    img = _img()
    out = s.rotate_sub_image_if_confident(img, FakeCls(), source="legacy.jpg")
    assert out.tobytes() == img.rotate(180, expand=True).tobytes()


def test_upstream_hook_delegates(monkeypatch):
    """upstream hook（pdf_image_tools._rotate_sub_image）已接入门控。"""
    _patch_vote(monkeypatch, "180", 0.403)
    monkeypatch.setattr(pit, "_get_orientation_cls_model", lambda: FakeCls())
    img = _img()
    assert pit._rotate_sub_image(img, source="hook.jpg") is img


def test_hook_survives_custom_import_failure(monkeypatch):
    """custom 模块不可用时 hook 退回"保留原图"，不得中断解析。"""
    monkeypatch.setitem(sys.modules, "mineru.utils.custom.sub_image_rotate", None)
    img = _img()
    with capture_logs(level="ERROR") as records:
        out = pit._rotate_sub_image(img, source="broken.jpg")
    assert out is img
    assert any("图块级旋转门控执行失败" in r for r in records)
