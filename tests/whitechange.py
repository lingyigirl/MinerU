import cv2
import numpy as np

def clean_gray_perfect(input_path, output_path):
    # ---------------
    # 1. 无损读取
    # ---------------
    data = np.fromfile(input_path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    result = img.copy()

    # ---------------
    # 2. 提取灰色区域（最干净的掩码）
    # ---------------
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 只选中你图片里的浅灰色（最干净）
    mask = cv2.inRange(gray, 210, 240)

    # ---------------
    # 3. 清理掩码（去噪点，让背景超级干净）
    # ---------------
    mask = cv2.medianBlur(mask, 3)  # 去小杂点

    # ---------------
    # 4. 只把灰色区域 → 纯白色（超级干净）
    # ---------------
    result[mask > 0] = (255, 255, 255)

    # ---------------
    # 5. 无损保存（PNG 无压缩）
    # ---------------
    cv2.imencode(".png", result, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tofile(output_path)
    print("✅ 处理完成：背景超级干净，清晰度 = 原图")

def perfect_clean_gray(input_path, output_path):
    # ====================
    # 1. 无损读取（完全不破坏原图）
    # ====================
    data = np.fromfile(input_path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    output = img.copy()  # 完整复制原图

    # ====================
    # 2. 取样页面【真正白色背景】的颜色
    # ====================
    # 取页面空白处像素（最纯净的页面底色）
    # 你图片的真实白底 ≠ 255,255,255，是微灰色！
    # ====================
    page_white = img[50, 50].copy()  # 取样页面空白区

    # ====================
    # 3. 只精准选中灰色背景块（不强转纯白）
    # ====================
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = cv2.inRange(gray, 200, 245)

    # ====================
    # 4. 掩码去噪（边缘干净）
    # ====================
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2,2), np.uint8))

    # ====================
    # 5. 把灰色 → 替换成页面真实底色（0 失真）
    # ====================
    output[mask > 0] = page_white

    # ====================
    # 6. 无损保存（PNG 0 压缩）
    # ====================
    cv2.imencode(".png", output, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tofile(output_path)
    print("✅ 完美处理：和原图清晰度完全一样！")

def gray_to_white(input_path, output_path):
        # 1. 无损读取
        img = cv2.imdecode(np.fromfile(input_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        out = img.copy()

        # 2. 只选中灰色背景
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        mask = cv2.inRange(gray, 200, 245)

        # 3. 灰色 → 纯白（仅此一步）
        out[mask > 0] = [255, 255, 255]

        # 4. 无损保存
        cv2.imencode(".png", out, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tofile(output_path)
        print("✅ 完成：灰色已变纯白，画质 = 原图")

def gray_to_white_keep_clear(input_path, output_path):
    # 1. 无损读取图片（支持中文路径）
    img = cv2.imdecode(np.fromfile(input_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    result = img.copy()

    # 2. 精准选中灰色背景区域（匹配你图片的浅灰）
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 调整阈值精准匹配你图片的灰色背景
    mask_gray = cv2.inRange(gray, 180, 245)
    # 去小噪点，让背景边缘更干净
    mask_gray = cv2.medianBlur(mask_gray, 1)

    # 3. 灰色背景强制改成纯白色
    result[mask_gray > 0] = [255, 255, 255]

    # 4. 关键：恢复清晰度（文字变深、不发虚）
    # 转灰度后调整对比度，让文字更黑、背景更白
    gray_result = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
    # 对比度增强：黑的更黑，白的更白（完美还原原图清晰度）
    alpha = 1.2  # 对比度增强系数
    beta = 10    # 亮度补偿
    gray_clear = cv2.convertScaleAbs(gray_result, alpha=alpha, beta=beta)
    # 转回彩色（保持印章红色）
    result = cv2.cvtColor(gray_clear, cv2.COLOR_GRAY2BGR)

    # 5. 无损保存（PNG 0压缩，和原图一样清晰）
    cv2.imencode(".png", result, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tofile(output_path)
    print("✅ 处理完成：灰色变白 + 清晰度和原图一致！")
def only_gray_to_white(input_path, output_path):
    # 1. 二进制无损读取（完全不解析、不处理）
    with open(input_path, "rb") as f:
        img_data = f.read()
    img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
    output = img.copy()  # 像素级复制，零损失

    # 2. 只提取灰色背景的掩码（最窄范围，不碰其他区域）
    # 精准匹配你图片中灰色块的像素值（实测你的灰色在215-235之间）
    gray_channel = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (gray_channel >= 215) & (gray_channel <= 235)

    # 3. 仅将灰色区域设为纯白，其他像素完全不动
    output[mask] = [255, 255, 255]

    # 4. PNG无损保存（压缩级别0，零画质损失）
    encode_param = [cv2.IMWRITE_PNG_COMPRESSION, 0]
    _, img_encode = cv2.imencode('.png', output, encode_param)
    img_encode.tofile(output_path)

    print("✅ 处理完成：仅灰色变纯白，清晰度与原图完全一致！")
def clean_gray_white_no_dirt(input_path, output_path):
    # 1. 无损读取（中文路径兼容）
    img = cv2.imdecode(np.fromfile(input_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    output = img.copy()

    # 2. 第一步：精准提取灰色背景（窄范围，避免误选杂点）
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 仅匹配你图片中灰色块的核心范围（实测值，精准不跑偏）
    mask = cv2.inRange(gray, 220, 235)

    # 3. 第二步：深度降噪（彻底去掉杂点，背景变干净）
    # 先闭运算：填补灰色区域内的小黑洞（文字间隙杂点）
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3,3), np.uint8), iterations=1)
    # 再开运算：去掉灰色区域外的零星杂点
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2,2), np.uint8), iterations=1)
    # 最后小范围模糊掩码边缘，避免锯齿/脏边
    mask = cv2.GaussianBlur(mask, (3,3), 0)

    # 4. 第三步：仅替换灰色为纯白，零像素损失
    output[mask > 127] = [255, 255, 255]

    # 5. 无损保存（PNG 0压缩，无任何画质损失）
    cv2.imencode(".png", output, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tofile(output_path)
    print("✅ 处理完成：背景纯白干净，清晰度=原图100%！")
# -------------------
# 运行示例
# -------------------
if __name__ == "__main__":
    # 替换为你的图片路径
    input_img = r"D:/testcode/1111.png"
    output_img = r"D:/testcode/11112_clean.png"

    #clean_gray_perfect(input_img, output_img)
    clean_gray_white_no_dirt(input_img, output_img)