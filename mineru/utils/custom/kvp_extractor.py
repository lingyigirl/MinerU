"""KVP Pipeline：基于 MLLM 的票据/卡证 Key-Value 信息提取器。

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
    temperature: float = 0.0  # 确定性输出，避免票据数字幻觉
    max_tokens: int = 2048  # 单页表单 2048 token 足够，过大容易诱发幻觉数字
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
    # 本地规则引擎（基于 PaddleOCR + 空间距离配对，离线可用）
    "pp-structure": KvpEngineConfig(
        engine="pp-structure",
        model_name="local-kvp-engine",
        # 本地引擎，无需 base_url / api_key
    ),
}


# ---------------------------------------------------------------------------
# 默认 KVP 提取 Prompt 模板
# ---------------------------------------------------------------------------

DEFAULT_KVP_PROMPT_TEMPLATE = """请严格按照图片中的文字内容，提取所有字段信息。

规则：
1. 只输出图片中实际出现的文字，绝不编造
2. 看不清的内容标注为 [无法识别]
3. 每个字段的值最多30个字符，超出的截断
4. 直接输出 JSON 对象，以 { 开头 } 结尾，不要用代码块包裹

示例输出格式：{"户名": "张三", "账号": "622700123456", "金额": "50000"}"""


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

    last_error = None
    for attempt in range(2):
        try:
            # 第二次尝试降低温度 + 缩短 prompt 防幻觉
            if attempt == 0:
                temp = config.temperature
                current_prompt = prompt
            else:
                temp = 0.0
                current_prompt = "按图片提取所有字段的标签和值，输出JSON。每个值最多20字符。不确定的标[?]。"

            response = client.chat.completions.create(
                model=config.model_name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": img_url}},
                        {"type": "text", "text": current_prompt},
                    ],
                }],
                temperature=temp,
                max_tokens=config.max_tokens,
            )
        except Exception:
            logger.exception(f"VLM API 调用失败 (engine={config.engine}, attempt={attempt})")
            raise RuntimeError(f"VLM API 调用失败: {config.engine}")

        content = response.choices[0].message.content
        logger.debug(f"VLM 响应 (attempt={attempt}, tokens={response.usage}): {content[:200]}...")

        try:
            return _parse_json_response(content)
        except ValueError as e:
            last_error = e
            logger.warning(f"VLM JSON 解析失败 (attempt={attempt}): {str(e)[:100]}")

    raise last_error  # type: ignore[misc]


def _parse_json_response(content: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON dict。

    处理常见的输出格式：
    1. 纯 JSON（以 `{` 开头）
    2. Markdown 代码块包裹（```json ... ```）
    3. 夹带解释文字的 JSON
    4. 被截断的不完整 JSON（尝试修复）

    Args:
        content: LLM 原始响应文本。

    Returns:
        解析后的 dict。

    Raises:
        ValueError: 无法提取有效 JSON。
    """
    # 尝试 1: Markdown 代码块
    md_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", content)
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
        pass

    # 尝试 3: 修复常见格式问题
    cleaned = _attempt_json_repair(content)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            logger.warning("JSON 经格式修复后成功解析")
            return result
    except json.JSONDecodeError:
        pass

    # 尝试 4: 截断恢复 — 补全缺失的引号、括号
    recovered = _attempt_truncation_recovery(cleaned)
    if recovered:
        try:
            result = json.loads(recovered)
            if isinstance(result, dict):
                logger.warning("JSON 经截断恢复后成功解析（最后一行数据可能不完整）")
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
    # 移除尾部逗号
    content = re.sub(r",\s*([}\]])", r"\1", content)
    content = re.sub(r",\s*$", "", content)
    return content


def _attempt_truncation_recovery(content: str) -> str | None:
    """尝试恢复被截断的不完整 JSON。

    当 VLM 输出被 max_tokens 截断时，JSON 可能缺少闭合的引号和括号。
    尝试从最后一个完整的键值对处截断并补全 JSON。

    Args:
        content: 可能不完整的 JSON 字符串。

    Returns:
        修复后的完整 JSON 字符串，如果无法修复则返回 None。
    """
    if not content.strip():
        return None

    # 如果已经以 } 结尾，不需要修复
    content = content.rstrip()
    if content.endswith("}"):
        return None

    # 找到最后一个完整的键值对（以 ", 结尾的行）
    # 回退到最后一个逗号或换行处
    last_comma = content.rfind(',\n')
    if last_comma == -1:
        last_comma = content.rfind(',\n  ')
    if last_comma == -1:
        return None

    # 截断到最后一个完整的键值对
    truncated = content[:last_comma].rstrip().rstrip(',')

    # 补全 JSON：移除尾部逗号，加上闭合括号
    truncated = re.sub(r",\s*$", "", truncated)

    # 计算未闭合的括号
    open_braces = truncated.count('{') - truncated.count('}')
    open_brackets = truncated.count('[') - truncated.count(']')

    # 检查最后一个值是否有未闭合的字符串
    # 如果最后一个 " 后面没有对应的 "，补一个 "
    last_line = truncated.split('\n')[-1].strip()
    quote_count = last_line.count('"')
    if quote_count % 2 != 0:
        truncated += '"'

    # 补全括号
    truncated += ']' * max(0, open_brackets)
    truncated += '}' * max(0, open_braces)

    return truncated


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def extract_kvp_from_form(
    pdf_bytes: bytes,
    engine: Optional[str] = None,
    server_url: Optional[str] = None,
    kvp_prompt: Optional[str] = None,
    verify_engine: Optional[str] = None,
    dpi: int = DEFAULT_PDF_IMAGE_DPI,
    start_page_id: int = 0,
    end_page_id: Optional[int] = None,
) -> dict[str, Any]:
    """KVP Pipeline 主入口：支持本地引擎 + 远程 VLM + 混合纠错模式。

    三种运行模式（通过 engine 参数或 KVP_ENGINE 环境变量控制）：
    - engine="pp-structure" (默认) → 纯本地 OCR + 规则配对（离线可用）
    - engine="pp-structure" + verify_engine="qwen-vl-max" → 本地主 + VLM 纠错
    - engine="qwen-vl-max" → 纯 VLM API（需外网）

    Args:
        pdf_bytes: PDF 文件字节流。
        engine: 主引擎标识符（None 则从 KVP_ENGINE 环境变量读取，默认 pp-structure）。
        server_url: 自定义 API 地址。
        kvp_prompt: 自定义 KVP 提取 prompt。
        verify_engine: 可选的 VLM 纠错引擎（None 表示不纠错）。
        dpi: 渲染 DPI。
        start_page_id: 起始页（0-based）。
        end_page_id: 结束页。

    Returns:
        MinerU 兼容的 middle_json 格式 dict。
    """
    # 默认引擎：参数 > 环境变量 KVP_ENGINE > pp-structure
    if engine is None:
        engine = os.environ.get("KVP_ENGINE", "pp-structure")

    # 渲染页面
    images = load_images_from_pdf_core(
        pdf_bytes, dpi=dpi, start_page_id=start_page_id, end_page_id=end_page_id,
    )
    if not images:
        raise RuntimeError("PDF 渲染后无有效页面")

    # ---- 路径 1：本地规则引擎 ----
    if engine == "pp-structure":
        from mineru.utils.custom.kvp_local_engine import extract_kvp_local

        all_page_results: list[dict[str, Any]] = []
        for page_idx, img_dict in enumerate(images):
            pil_img = img_dict["img_pil"]
            try:
                page_kvp = extract_kvp_local(pil_img)
                all_page_results.append(page_kvp)
                logger.debug(
                    f"Page {page_idx + 1}/{len(images)} 本地 KVP: "
                    f"{len(page_kvp)} 个字段"
                )
            except Exception:
                logger.exception(f"Page {page_idx + 1} 本地 KVP 提取失败")
                all_page_results.append({"_error": f"Page {page_idx + 1} 提取失败"})

        # [可选] VLM 纠错
        if verify_engine and verify_engine in ENGINE_REGISTRY:
            all_page_results = _verify_with_vlm(
                images, all_page_results, verify_engine, server_url, kvp_prompt,
            )

        middle_json = _convert_kvp_to_middle_json(all_page_results, images, engine)
        logger.info(
            f"KVP 提取完成: {len(all_page_results)} 页, "
            f"总字段数 {sum(len(r) for r in all_page_results)}"
        )
        return middle_json

    # ---- 路径 2：远程 VLM 引擎（原有逻辑） ----
    if engine not in ENGINE_REGISTRY:
        raise ValueError(
            f"不支持的 KVP 引擎: {engine}。"
            f"可用引擎: {list(ENGINE_REGISTRY.keys())}"
        )
    config = ENGINE_REGISTRY[engine]

    effective_url = server_url or os.environ.get("KVP_SERVER_URL")
    if effective_url:
        config = KvpEngineConfig(
            engine=config.engine,
            model_name=config.model_name,
            base_url=effective_url,
            api_key=config.api_key,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )

    prompt = kvp_prompt or DEFAULT_KVP_PROMPT_TEMPLATE

    logger.info(
        f"开始 KVP 提取: engine={engine}, pages={len(images)}, "
        f"dpi={dpi}, remote={config.is_remote()}"
    )

    all_page_results = []
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

    middle_json = _convert_kvp_to_middle_json(all_page_results, images, engine)

    logger.info(
        f"KVP 提取完成: {len(all_page_results)} 页, "
        f"总字段数 {sum(len(r) for r in all_page_results)}"
    )
    return middle_json


# ---------------------------------------------------------------------------
# VLM 纠错
# ---------------------------------------------------------------------------

def _verify_with_vlm(
    images: list[dict],
    page_results: list[dict[str, Any]],
    verify_engine: str,
    server_url: Optional[str] = None,
    kvp_prompt: Optional[str] = None,
) -> list[dict[str, Any]]:
    """使用 VLM 对本地 KVP 提取结果进行高价值字段二次确认。

    仅对金额、姓名、账号、日期等高价值字段调用 VLM 验证。
    VLM 结果与本地结果不一致时，保留两者并标记差异。

    Args:
        images: 已渲染的页面图片列表。
        page_results: 本地引擎的 KVP 提取结果。
        verify_engine: 验证用的 VLM 引擎标识。
        server_url: 自定义 API 地址。
        kvp_prompt: 自定义 prompt。

    Returns:
        合并后的 page_results 列表。
    """
    logger.info(f"开始 VLM 纠错: engine={verify_engine}")

    HIGH_VALUE_KEYS = {
        "户名", "账号", "存入金额(大写)", "存入金额(小写)",
        "金额(大写)", "金额(小写)", "金额", "证件号码",
        "客户号", "开户日", "起息日", "到期日",
    }

    config = ENGINE_REGISTRY[verify_engine]
    effective_url = server_url or os.environ.get("KVP_SERVER_URL")
    if effective_url:
        config = KvpEngineConfig(
            engine=config.engine,
            model_name=config.model_name,
            base_url=effective_url,
            api_key=config.api_key,
            temperature=0.0,  # 纠错模式使用确定性输出
            max_tokens=config.max_tokens,
            timeout=config.timeout,
        )

    verify_prompt = """请仔细辨认图片中的以下关键字段，输出JSON：
- 户名（姓名）
- 账号
- 存入金额的大写和小写
- 开户日和到期日
- 客户号

直接输出JSON，只包含你能清晰辨认的字段。不确定的字段不要输出。"""

    for page_idx, img_dict in enumerate(images):
        if page_idx >= len(page_results):
            break

        pil_img = img_dict["img_pil"]
        local_kvp = page_results[page_idx]

        try:
            vlm_kvp = _call_vlm_api(pil_img, verify_prompt, config)

            # 对比差异
            differences = {}
            for key in HIGH_VALUE_KEYS:
                local_val = local_kvp.get(key)
                vlm_val = vlm_kvp.get(key)
                if local_val and vlm_val and local_val != vlm_val:
                    differences[key] = {"local": local_val, "vlm": vlm_val}
                    logger.warning(
                        f"VLM 纠错差异 [{key}]: local={local_val}, vlm={vlm_val}"
                    )

            if differences:
                local_kvp["_vlm_differences"] = differences
                logger.info(f"Page {page_idx + 1} VLM 纠错发现 {len(differences)} 处差异")

        except Exception:
            logger.exception(f"Page {page_idx + 1} VLM 纠错失败，保留本地结果")

    return page_results


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
                "source": f"kvp_{engine}",
            }
        ]

        pdf_info.append({
            "page_idx": page_idx,
            "width": w,
            "height": h,
            "spans": spans,
            "_kvp_raw": kvp_dict,  # 保留原始 KVP 结果
        })

    return {
        "pdf_info": pdf_info,
        "_backend": "kvp",
        "_kvp_engine": engine,
    }
