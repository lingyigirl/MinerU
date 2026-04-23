import asyncio
from typing import Optional

import httpx
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
import io
import logging

from PIL.JpegImagePlugin import JpegImageFile

logger = logging.getLogger(__name__)

# ========================
# 印章接口配置（你自己改）
# ========================
#from mineru.utils.config_reader import get_ocr_config
#seal_ocr = get_ocr_config()
SEAL_API_URL = "http://172.19.0.3:8008/recognize_seal"
#SEAL_API_URL = seal_ocr.get('base_url','http://172.19.0.3:8008/recognize_seal')
SEAL_MAX_LONG_EDGE = 1280  # 图片优化长边
SEAL_QUALITY = 85
SEAL_DEFAULT_FORMAT="JPEG"
def optimize_image_for_seal_detect(image: Image.Image) -> bytes:
    """印章接口专用：压缩 + 缩放 + 转 JPEG"""
    SEAL_MAX_LONG_EDGE = 1024
    SEAL_QUALITY = 85

    w, h = image.size
    scale = SEAL_MAX_LONG_EDGE / max(w, h)
    if scale < 1:
        new_w = int(w * scale)
        new_h = int(h * scale)
        image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # 处理透明图，防止保存失败
    if image.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        bg.paste(image, mask=image.split()[-1])
        image = bg

    # 压缩成 JPEG 字节
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=SEAL_QUALITY, optimize=True)
    return buf.getvalue()

def restore_seal_bbox(
    original_w: int,
    original_h: int,
    seal_bbox: list,
    max_long_edge: int = 1080
) -> list:
    """
    把缩小后的 bbox 还原成原始图片坐标
    """
    w, h = original_w, original_h
    scale = max_long_edge / max(w, h)

    if scale >= 1:
        return seal_bbox

    # 还原坐标
    x1, y1, x2, y2 = seal_bbox
    x1 = int(x1 / scale)
    y1 = int(y1 / scale)
    x2 = int(x2 / scale)
    y2 = int(y2 / scale)
    return [x1, y1, x2, y2]


def optimize_image_for_llm_recognize(image: Image.Image) -> bytes:
    """
    大模型 / 印章识别 专用图片优化
    极大提高识别成功率！
    """
    # ==================== 1. 统一格式 ====================
    img = image.convert("RGB")

    # ==================== 2. 提取红色通道（印章最强优化） ====================
    r, g, b = img.split()
    img = r  # 只保留红色，印章最突出

    # ==================== 3. 自动裁剪：只保留印章区域（去掉背景） ====================
    img_np = np.array(img)
    # 自动二值化，找到印章区域
    mask = img_np < 200  # 红色印章会变成深色
    coords = np.argwhere(mask)
    if len(coords) > 0:
        y0, x0 = coords.min(axis=0)
        y1, x1 = coords.max(axis=0)
        # 外扩10像素，防止切到文字
        y0 = max(0, y0 - 10)
        x0 = max(0, x0 - 10)
        y1 = min(img_np.shape[0], y1 + 10)
        x1 = min(img_np.shape[1], x1 + 10)
        img = Image.fromarray(img_np[y0:y1, x0:x1])

    # ==================== 4. 锐化：让文字更清晰 ====================
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.0)  # 锐化2倍，文字更清楚
    img.save("processed_seal.jpg")  # 👈 直接加这一行
    # ==================== 5. 调整尺寸 ====================
    max_size = 1024
    w, h = img.size
    if max(w, h) > max_size:
        scale = max_size / max(w, h)
        img = img.resize((int(w*scale), int(h*scale)), Image.Resampling.LANCZOS)

    # ==================== 6. 高质量输出 ====================
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, optimize=True)
    return buf.getvalue()


def optimize_image_for_seal_llm(image: Image.Image) -> bytes:
    """
    印章识别专用增强：
    去噪 + 对比度增强 + 锐化 + 二值化
    大幅提升大模型识别成功率
    """
    # 1. 统一转 RGB
    img = image.convert("RGB")

    # 2. 提取红色通道（印章最强增强）
    r, g, b = img.split()
    img = r

    # 3. 去噪（高斯模糊去噪点）
   #  img = img.filter(ImageFilter.GaussianBlur(radius=0.6))

    # 4. 对比度增强（文字更突出）
    #enhancer = ImageEnhance.Contrast(img)
    #img = enhancer.enhance(2.0)

    # 5. 锐化（让文字边缘清晰）
    #enhancer = ImageEnhance.Sharpness(img)
    #img = enhancer.enhance(3.0)

    # 6. 二值化（黑白分明，大模型最爱）
    #img_np = np.array(img)
    # 自适应阈值二值化
    #threshold = 160  # 可微调：印章浅就调小，深就调大
    #img_np = np.where(img_np > threshold, 255, 0).astype(np.uint8)
    #img = Image.fromarray(img_np)

    # 7. 保存处理后的图给你看效果
    img.save("seal_after_process4.jpg")

    # 8. 转字节
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, optimize=True)
    return buf.getvalue()


def optimize_image_for_copy_seal_light(image: Image.Image) -> bytes:
    img = image.convert("RGB")
    r, g, b = img.split()
    img = r

    # 去噪
    img = img.filter(ImageFilter.MedianFilter(2))
    # 对比度拉满
    img = ImageEnhance.Contrast(img).enhance(3.5)
    # 锐化
    img = ImageEnhance.Sharpness(img).enhance(2)

    # 固定阈值（复印章一般偏灰，阈值调低）
    img_np = np.array(img)
    threshold = 100  # 淡印再改小：100~130
    img_np = np.where(img_np > threshold, 255, 0).astype(np.uint8)
    img = Image.fromarray(img_np)

    img.save("seal_copy_result.jpg")

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95)
    return buf.getvalue()


def optimize_seal_for_llm(image: Image.Image) -> bytes:
    """
    智能印章优化：
    自动判断 → 正常章 / 复印淡章
    分别做对应增强，大幅提高大模型识别率
    """
    # 1. 统一转 RGB
    img = image.convert("RGB")

    # 2. 提取红色通道（印章核心通道）
    r, g, b = img.split()
    gray = np.array(r)

    # ======================
    # 自动判断：是否为复印淡章
    # ======================
    # 计算红色通道平均亮度
    mean_bright = gray.mean()
    # 计算对比度（方差）
    std_val = gray.std()
    # 复印章特点：偏灰、亮度高、对比度低
    is_copy_seal = mean_bright > 180 or std_val < 25

    # ======================
    # 通用基础增强
    # ======================
    img_pil = Image.fromarray(gray)

    # 去噪
    img_pil = img_pil.filter(ImageFilter.MedianFilter(size=3))

    # ======================
    # 分支处理
    # ======================
    if is_copy_seal:
        # ========== 复印章：强力增强 ==========
        print("检测到【复印淡章】，使用强化模式")
        # 对比度拉满
        img_pil = ImageEnhance.Contrast(img_pil).enhance(3.2)
        # 锐化
        img_pil = ImageEnhance.Sharpness(img_pil).enhance(2.2)
        # 低阈值二值化
        thresh = 125
    else:
        # ========== 正常鲜章：清晰干净 ==========
        print("检测到【正常鲜章】，使用标准模式")
        # 适度增强
        img_pil = ImageEnhance.Contrast(img_pil).enhance(1.8)
        img_pil = ImageEnhance.Sharpness(img_pil).enhance(1.5)
        # 高阈值二值化
        thresh = 160

    # ======================
    # 二值化（黑白）
    # ======================
    img_np = np.array(img_pil)
    img_np = np.where(img_np > thresh, 255, 0).astype(np.uint8)
    final = Image.fromarray(img_np)

    # 保存处理后的图查看效果
    final.save("seal_final.jpg")

    # 转字节流给接口
    buf = io.BytesIO()
    final.save(buf, format="JPEG", quality=95, optimize=True)
    return buf.getvalue()


def image_to_raw_bytes(image):
    """
    打开图片，不做任何处理，直接转成字节流
    完全等于直接上传原文件
    """
    # 1. 打开图片
    # img = Image.open(image_path)

    # 2. 直接转成图片字节（无处理、无压缩、无修改）
    buf = io.BytesIO()
    image.save(buf, format=image.format)  # 保存原格式（JPG/PNG）
    img_bytes = buf.getvalue()

    return img_bytes

# ======================
# 印章外部接口调用
# ======================
async def call_seal_api(page_image:Image.Image) -> list[dict]:
    """
        传入整页图片 -> 调用外部接口 -> 返回印章列表
    """
    try:
        # 转换图片字节
        #png_bytes = optimize_image_for_seal_detect(page_image)
        #png_bytes = optimize_image_for_llm_recognize(page_image)
        #png_bytes = optimize_image_for_copy_seal_light(page_image)
        print('page_image=================',page_image)
        png_bytes = image_to_raw_bytes(page_image)
        # 发送请求
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url=SEAL_API_URL,
                files={"file":("seal.jpg",png_bytes,"image/jpeg")},
                )
        if resp.status_code != 200:
            return []
        result = resp.json()
        code = result["code"]
        print('result',result)
        # 失败
        if code != 0:
            logger.error('调用外部识别印章接口失败：',result['msg'])
            return []
        else:
            # 获取识别结果
            data = result['data']
            if data['seal_text_list'] and len(data['seal_text_list']) > 0:
                return data['seal_text_list']
            else:
                return []
    except Exception as e:
        print("[印章接口]调用失败：",e)
        return []


# ========================
# 【独立印章处理函数】
# ========================
async def add_seal_results_to_pages(
    images_pil: Image.Image,
    results: list[list[dict]]
):
    """
    统一给所有页添加印章结果
    总控只需要调用这一行！
    """
    try:
        print("开始处理印章...")
        # 1. 调用印章接口
        seal_list = await call_seal_api(images_pil)
        results.append(seal_list)



        logger.info("印章处理完成 ✅")

    except Exception as e:
        logger.error(f"印章处理异常: {e}")


# ========================
# 👇👇👇 测试方法 👇👇👇
# ========================
async def test_seal_api(page_image):
    """
    印章接口独立测试方法
    1. 加载一张图片
    2. 调用你的印章函数
    3. 打印结果
    """
    print("=" * 50)
    print("开始测试印章接口")
    print("=" * 50)

    # -------------------
    # 1. 加载测试图片（改成你本地图片路径）
    # -------------------
    #image_path = "D:/download/linuxdownfile/33.jpg"  # <-- 改成你带印章的图片
    #page_image = Image.open(image_path).convert("RGB")

    # -------------------
    # 2. 构造假数据结构
    # -------------------
    # images_list = [page_image]
    results = []  # 一页，空块

    # -------------------
    # 3. 调用你的印章函数
    # -------------------
    await add_seal_results_to_pages(page_image, results)

    # -------------------
    # 4. 打印结果
    # -------------------
    print("\n【测试完成，返回结果】")
    return results[0]
# -------------- 改成下面这样 --------------

def run_test_seal_api(page_image):
    """
    外部同步调用入口
    返回：test_seal_api() 异步函数的返回值
    """
    import asyncio
    # 🔥 关键：run() 会自动等待并返回异步函数的结果
    return asyncio.run(test_seal_api(page_image))


# ========================
# 运行测试
# ========================
if __name__ == "__main__":
    image_path = "D:/download/linuxdownfile/33.jpg"  # <-- 改成你带印章的图片
    page_image = Image.open(image_path)
    print("===========page_image==============",page_image)
    pi = page_image.resize((100,100))
    print("===========pi==============", pi)
    if isinstance(page_image,JpegImageFile):
        print("1")
    else :
        print("2")
    #result = run_test_seal_api(pi)
    #print('result=======',result)

