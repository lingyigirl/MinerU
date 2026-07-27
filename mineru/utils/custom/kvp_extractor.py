"""KIE Pipeline：基于 MLLM 的票据/卡证 Key-Value 信息提取器。

支持的引擎：
- qwen-vl-plus / qwen-vl-max：阿里云 DashScope API（OpenAI 兼容）
- qwen-vl-local：本地 vLLM 部署的 Qwen2.5-VL-7B
- internvl-local：本地 LMDeploy 部署的 InternVL2.5-8B
- pp-structure：本地 PaddleOCR PP-StructureV3（预留）

架构遵循 OpenAI 兼容 API 协议，本地和云端引擎使用相同的调用接口。
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Optional

from loguru import logger
from PIL import Image

from mineru.utils.pdf_image_tools import (
    DEFAULT_PDF_IMAGE_DPI,
    load_images_from_pdf_core,
)


# ---------------------------------------------------------------------------
# 引擎配置
# ---------------------------------------------------------------------------

@dataclass
class KvpEngineConfig:
    """KVP 引擎配置。

    Attributes:
        engine: 引擎标识符（如 "qwen-vl-plus"）。
        model_name: API 调用时使用的模型名称。
        base_url: API 地址（None 表示使用默认云端点）。
        api_key: API 密钥（None 表示从环境变量读取）。
        temperature: 采样温度（0.0 ~ 1.0），越低越确定性。
        max_tokens: 最大输出 token 数。
        timeout: 请求超时秒数。
    """

    engine: str
    model_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: int = 60

    def is_remote(self) -> bool:
        """是否使用远程云 API。"""
        return self.engine in ("qwen-vl-plus", "qwen-vl-max")


# 引擎注册表
ENGINE_REGISTRY: dict[str, KvpEngineConfig] = {
    # 阿里云 DashScope API
    "qwen-vl-plus": KvpEngineConfig(
        engine="qwen-vl-plus",
        model_name="qwen-vl-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "qwen-vl-max": KvpEngineConfig(
        engine="qwen-vl-max",
        model_name="qwen-vl-max",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    # 本地 vLLM（默认端口 30001）
    "qwen-vl-local": KvpEngineConfig(
        engine="qwen-vl-local",
        model_name="Qwen/Qwen2.5-VL-7B-Instruct",
        base_url="http://localhost:30001/v1",
    ),
    # 本地 LMDeploy（默认端口 30002）
    "internvl-local": KvpEngineConfig(
        engine="internvl-local",
        model_name="OpenGVLab/InternVL2_5-8B",
        base_url="http://localhost:30002/v1",
    ),
}


# ---------------------------------------------------------------------------
# 默认 KVP 提取 Prompt 模板
# ---------------------------------------------------------------------------

DEFAULT_KVP_PROMPT_TEMPLATE = """请提取这张文档图片中所有的"标签-值"（Key-Value）信息。

要求：
1. 仔细识别图片中每一个字段的标签（如：户名、账号、日期、金额等）及其对应的值
2. 输出纯 JSON 格式，Key 为标签（中文），Value 为对应的取值
3. 不要遗漏任何信息，包括印章中的文字、手写内容、印刷文字
4. 如果某个值看不清或不完整，请标注为 "[无法识别]" 而不是编造内容
5. 数字和日期请保持原始格式，不要做任何改动
6. 大写金额和小写金额都要提取

输出格式：
{
  "字段1": "值1",
  "字段2": "值2",
  ...
}"""


# ---------------------------------------------------------------------------
# 图片处理
# ---------------------------------------------------------------------------

def _pil_to_base64_url(pil_img: Image.Image, fmt: str = "JPEG", quality: int = 95) -> str:
    """将 PIL Image 转换为 data URL 字符串。

    Args:
        pil_img: PIL Image 对象。
        fmt: 图片格式（JPEG / PNG）。
        quality: JPEG 质量。

    Returns:
        data:image/...;base64,... 格式的 URL 字符串。
    """
    buf = BytesIO()
    # 确保 RGB 模式（JPEG 不支持 RGBA）
    if fmt.upper() == "JPEG" and pil_img.mode in ("RGBA", "P"):
        pil_img = pil_img.convert("RGB")
    pil_img.save(buf, format=fmt, quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/{fmt.lower()};base64,{b64}"


# ---------------------------------------------------------------------------
# OpenAI 兼容 API 调用
# ---------------------------------------------------------------------------

def _create_openai_client(config: KvpEngineConfig):
    """创建 OpenAI 兼容客户端。

    Args:
        config: 引擎配置。

    Returns:
        openai.OpenAI 实例。
    """
    from openai import OpenAI

    api_key = (
        config.api_key
        or os.environ.get("KVP_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or "not-needed"
    )
    # base_url 也可以从环境变量覆盖
    base_url = os.environ.get("KVP_SERVER_URL") or config.base_url
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=config.timeout,
    )


def _call_vlm_api(
    pil_img: Image.Image,
    prompt: str,
    config: KvpEngineConfig,
) -> dict[str, Any]:
    """调用 VLM API 进行 KVP 提取。

    Args:
        pil_img: 要分析的图片。
        prompt: 提取 prompt。
        config: 引擎配置。

    Returns:
        解析后的 JSON dict。

    Raises:
        RuntimeError: API 调用失败或 JSON 解析失败。
    """
    client = _create_openai_client(config)
    img_url = _pil_to_base64_url(pil_img)

    try:
        response = client.chat.completions.create(
            model=config.model_name,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": img_url}},
                    {"type": "text", "text": prompt},
                ],
            }],
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    except Exception:
        logger.exception(f"VLM API 调用失败 (engine={config.engine})")
        raise RuntimeError(f"VLM API 调用失败: {config.engine}")

    content = response.choices[0].message.content
    logger.debug(f"VLM 响应 (tokens={response.usage}): {content[:200]}...")

    # 尝试从响应中提取 JSON
    return _parse_json_response(content)


def _parse_json_response(content: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON dict。

    处理常见的输出格式：
    1. 纯 JSON（以 `{` 开头）
    2. Markdown 代码块包裹（```json ... ```）
    3. 夹带解释文字的 JSON

    Args:
        content: LLM 原始响应文本。

    Returns:
        解析后的 dict。

    Raises:
        ValueError: 无法提取有效 JSON。
    """
    # 尝试 1: Markdown 代码块
    md_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n```", content)
    if md_match:
        content = md_match.group(1).strip()

    # 尝试 2: 找到第一个 { 和最后一个 } 之间的内容
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]

    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return result
        raise ValueError(f"JSON 不是 dict 类型: {type(result)}")
    except json.JSONDecodeError:
        # 尝试修复常见的 JSON 格式问题
        cleaned = _attempt_json_repair(content)
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                logger.warning("JSON 经修复后成功解析")
                return result
        except json.JSONDecodeError:
            pass
        raise ValueError(f"无法解析 VLM 响应为 JSON: {content[:500]}")


def _attempt_json_repair(content: str) -> str:
    """尝试修复常见 JSON 格式错误。

    Args:
        content: 可能包含格式错误的 JSON 字符串。

    Returns:
        修复后的字符串。
    """
    # 移除尾部逗号（最常见的问题）
    content = re.sub(r",\s*([}\]])", r"\1", content)
    # 移除尾部多余的逗号（最后一个值后）
    content = re.sub(r",\s*$", "", content)
    return content


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def extract_kvp_from_form(
    pdf_bytes: bytes,
    engine: str = "qwen-vl-plus",
    server_url: Optional[str] = None,
    kvp_prompt: Optional[str] = None,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,
    start_page_id: int = 0,
    end_page_id: Optional[int] = None,
) -> dict[str, Any]:
    """使用专用 MLLM 提取票据/卡证的 KVP 信息（KIE Pipeline 主入口）。

    流程：
    1. 渲染 PDF 页面为图片
    2. 逐页调用 VLM API 提取 KVP
    3. 汇总并转换为 MinerU 兼容的 middle_json 格式

    Args:
        pdf_bytes: PDF 文件字节流。
        engine: 引擎标识符（"qwen-vl-plus" / "qwen-vl-local" / ...）。
        server_url: 自定义 API 地址（覆盖引擎默认配置）。
        kvp_prompt: 自定义 KVP 提取 prompt（None 则使用默认模板）。
        dpi: 渲染 DPI。
        start_page_id: 起始页（0-based）。
        end_page_id: 结束页（None 表示到最后一页）。

    Returns:
        MinerU 兼容的 middle_json 格式 dict，包含提取的 KVP 信息。

    Raises:
        ValueError: 不支持的引擎。
        RuntimeError: KVP 提取失败。
    """
    # 1. 获取引擎配置
    if engine not in ENGINE_REGISTRY:
        raise ValueError(
            f"不支持的 KVP 引擎: {engine}。"
            f"可用引擎: {list(ENGINE_REGISTRY.keys())}"
        )
    config = ENGINE_REGISTRY[engine]

    # 覆盖自定义 URL（参数优先，其次环境变量 KVP_SERVER_URL）
    effective_url = server_url or os.environ.get("KVP_SERVER_URL")
    if effective_url:
        config = KvpEngineConfig(
            engine=config.engine,
            model_name=config.model_name,
            base_url=server_url,
            api_key=config.api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )

    prompt = kvp_prompt or DEFAULT_KVP_PROMPT_TEMPLATE

    # 2. 渲染页面
    images = load_images_from_pdf_core(
        pdf_bytes, dpi=dpi, start_page_id=start_page_id, end_page_id=end_page_id,
    )
    if not images:
        raise RuntimeError("PDF 渲染后无有效页面")

    logger.info(
        f"开始 KVP 提取: engine={engine}, pages={len(images)}, "
        f"dpi={dpi}, remote={config.is_remote()}"
    )

    # 3. 逐页提取 KVP
    all_page_results: list[dict[str, Any]] = []
    for page_idx, img_dict in enumerate(images):
        pil_img = img_dict["img_pil"]
        try:
            page_kvp = _call_vlm_api(pil_img, prompt, config)
            all_page_results.append(page_kvp)
            logger.debug(
                f"Page {page_idx + 1}/{len(images)} KVP 提取完成: "
                f"{len(page_kvp)} 个字段"
            )
        except Exception:
            logger.exception(f"Page {page_idx + 1} KVP 提取失败")
            all_page_results.append({"_error": f"Page {page_idx + 1} 提取失败"})

    # 4. 转换为 MinerU middle_json 格式
    middle_json = _convert_kvp_to_middle_json(all_page_results, images, engine)

    logger.info(
        f"KVP 提取完成: {len(all_page_results)} 页, "
        f"总字段数 {sum(len(r) for r in all_page_results)}"
    )
    return middle_json


def _convert_kvp_to_middle_json(
    page_results: list[dict[str, Any]],
    images: list[dict],
    engine: str,
) -> dict[str, Any]:
    """将 KVP 提取结果转换为 MinerU 兼容的 middle_json 格式。

    生成的 middle_json 包含 pdf_info 数组，每页为一个 KVP block，
    使得下游的 Markdown/JSON 生成管线可以统一处理。

    Args:
        page_results: 每页的 KVP 提取结果。
        images: 渲染后的图片列表（用于获取尺寸信息）。
        engine: 使用的引擎标识。

    Returns:
        MinerU 兼容的 middle_json 格式。
    """
    pdf_info = []
    for page_idx, (kvp_dict, img_dict) in enumerate(zip(page_results, images)):
        pil_img = img_dict["img_pil"]
        w, h = pil_img.size

        # 将 KVP 转换为文本 spans（每个 KVP 对为一行）
        lines = []
        for key, value in kvp_dict.items():
            if key.startswith("_"):
                continue  # 跳过元数据字段
            lines.append(f"{key}: {value}")

        spans = [
            {
                "type": "text",
                "text": "\n".join(lines),
                "bbox": [0, 0, w, h],
                "page_idx": page_idx,
                "source": f"kie_{engine}",
            }
        ]

        pdf_info.append({
            "page_idx": page_idx,
            "width": w,
            "height": h,
            "spans": spans,
            "_kie_raw": kvp_dict,  # 保留原始 KVP 结果
        })

    return {
        "pdf_info": pdf_info,
        "_backend": "kie",
        "_kie_engine": engine,
    }
