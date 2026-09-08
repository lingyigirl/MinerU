#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 S0/S1 分类器新增的视觉特征（回归测试）。

覆盖 Phase 4：
- _detect_table_lines_ratio：cv2 形态学检测表格线密度（合成图像，确定性）
- 样本库准确率回归：读 manifest.json 对 data/ 下真实样本跑 classify_document，
  输出分类型准确率（PDF 缺失时跳过，报告式、不硬断言）
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mineru.utils.custom.doc_classifier import _detect_table_lines_ratio


def _table_image(w=800, h=600):
    """合成一张带横/竖表格线的灰度图（白底黑线）。"""
    import numpy as np
    from PIL import Image
    img = np.full((h, w), 255, dtype=np.uint8)
    for y in range(0, h, 50):
        img[y:y + 2, :] = 0          # 横线
    for x in range(0, w, 100):
        img[:, x:x + 2] = 0          # 竖线
    return Image.fromarray(img)


def _blank_image(w=800, h=600):
    import numpy as np
    from PIL import Image
    return Image.fromarray(np.full((h, w), 255, dtype=np.uint8))


def test_detect_table_lines_table():
    """表格线密集的图像应检测到正的线密度。"""
    ratio = _detect_table_lines_ratio([{"img_pil": _table_image()}], [0])
    assert ratio > 0.001, f"表格线应被检测到，实际 ratio={ratio:.4%}"


def test_detect_table_lines_blank():
    """空白图应检测到 0 线密度。"""
    ratio = _detect_table_lines_ratio([{"img_pil": _blank_image()}], [0])
    assert ratio == 0.0, f"空白图线密度应为 0，实际 {ratio}"


def _run_accuracy_report():
    """读取样本库 manifest，对存在的 PDF 跑 classify_document 并输出准确率。"""
    import json
    from mineru.utils.custom.doc_classifier import classify_document

    manifest_path = os.path.join(os.path.dirname(__file__), "classification_samples", "manifest.json")
    if not os.path.exists(manifest_path):
        print("样本库 manifest 不存在，跳过准确率回归")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    repo_root = os.path.join(os.path.dirname(__file__), "..")
    correct, total, skipped = 0, 0, 0
    results = []
    for sample in manifest.get("samples", []):
        pdf_path = os.path.join(repo_root, sample["path"])
        if not os.path.exists(pdf_path):
            skipped += 1
            continue
        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            doc_type = classify_document(pdf_bytes)
            actual = doc_type.value
            expected = sample["expected"]
            ok = actual == expected
            if ok:
                correct += 1
            total += 1
            results.append(f"  [{'✓' if ok else '✗'}] {os.path.basename(pdf_path)}: "
                           f"期望={expected} 实际={actual}")
        except Exception as e:  # noqa: BLE001
            skipped += 1
            results.append(f"  [跳过] {os.path.basename(pdf_path)}: 分类异常 {e}")

    print("分类准确率回归结果：")
    for r in results:
        print(r)
    if total > 0:
        print(f"准确率: {correct}/{total} = {correct / total:.1%}（跳过 {skipped} 个）")
    else:
        print(f"无可用的本地样本（跳过 {skipped} 个）")


if __name__ == "__main__":
    test_detect_table_lines_table()
    test_detect_table_lines_blank()
    print("S0/S1 视觉特征回归测试通过")
    print()
    _run_accuracy_report()
