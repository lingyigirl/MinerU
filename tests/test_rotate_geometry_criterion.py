"""旋转几何判据的隔离验证（monkeypatch 掉分类器与 PDF 渲染）。"""
import sys

from PIL import Image

import mineru.utils.custom.pdf_utils as pu


class FakeCls:
    """labels/dir 顺序：'0' 正，'90' 旋转。"""

    labels = ["0", "90", "180", "270"]

    def __init__(self, ocr_engine=None):
        pass

    def rotate_pil_image(self, img, label):
        return img.rotate(-int(label), expand=True)


def _imgs(sizes):
    return [{"img_pil": Image.new("RGB", s)} for s in sizes]


def _run(monkeypatch, sizes, votes, out):
    """votes: list[(label, conf)]；out: 记录被旋转的下标。"""
    imgs = _imgs(sizes)

    def fake_load(pdf_bytes, **kw):
        return imgs

    def fake_to_pdf(images_list, dpi):
        return b"%PDF-FAKE"

    monkeypatch.setattr(pu, "load_images_from_pdf_core", fake_load)
    monkeypatch.setattr(pu, "_images_to_pdf_bytes_with_dpi", fake_to_pdf)
    monkeypatch.setitem(
        sys.modules,
        "mineru.model.ori_cls.paddle_ori_cls",
        type(sys)("m"),
    )
    sys.modules["mineru.model.ori_cls.paddle_ori_cls"].PaddleOrientationClsModel = FakeCls
    monkeypatch.setattr(
        pu,
        "_get_classified_rotation",
        lambda cls, pil: votes.pop(0),
    )
    return imgs, fake_to_pdf(imgs, 200), pu.generate_rotation_corrected_pdf(b"x")


def test_geometry_rescues_low_confidence_true_rotate(monkeypatch):
    """真旋转页：conf 0.4354 < theta 0.45，几何异常 → 仍旋转。"""
    # 5 页 4 横 1 纵；多数派 label=0；纵页 label=90 conf=0.4354
    sizes = [(1000, 700)] * 4 + [(700, 1000)]
    votes = [("0", 0.9)] * 4 + [("90", 0.4354)]
    _, _, result = _run(monkeypatch, sizes, votes, None)
    assert result == b"%PDF-FAKE", "几何异常页应被旋转 → 触发重编码"


def test_geometry_does_not_flip_when_classifier_says_upright(monkeypatch):
    """几何异常但 label=='0'（分类器认为方向正确）→ 不旋转，保留原 PDF。"""
    sizes = [(1000, 700)] * 4 + [(700, 1000)]
    votes = [("0", 0.9)] * 4 + [("0", 0.30)]
    _, _, result = _run(monkeypatch, sizes, votes, None)
    assert result == b"", "仅几何证据不足以翻转分类器判定为正的页"


def test_no_geometry_evidence_falls_back_to_theta(monkeypatch):
    """全部横向（无几何异常）→ 低置信偏离页仍按 theta 否决。"""
    sizes = [(1000, 700)] * 5
    votes = [("0", 0.9)] * 4 + [("90", 0.4354)]
    _, _, result = _run(monkeypatch, sizes, votes, None)
    assert result == b"", "几何证据不存在时应退化为纯阈值判定（原行为）"


def test_geometry_disabled_by_ratio(monkeypatch):
    """多数派占比不足（3 横 2 纵 = 0.6 < 0.8）→ 判据不生效。"""
    sizes = [(1000, 700)] * 3 + [(700, 1000)] * 2
    votes = [("0", 0.9)] * 3 + [("90", 0.4354), ("90", 0.42)]
    _, _, result = _run(monkeypatch, sizes, votes, None)
    assert result == b"", "混排文档不应启用几何判据"


def test_geometry_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MINERU_ROTATE_GEOM_ENABLE", "false")
    sizes = [(1000, 700)] * 4 + [(700, 1000)]
    votes = [("0", 0.9)] * 4 + [("90", 0.4354)]
    _, _, result = _run(monkeypatch, sizes, votes, None)
    assert result == b"", "开关关闭时应退化为纯阈值判定"
