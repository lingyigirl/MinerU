"""引擎工厂 + 策略选择器。

根据 S0 质量分析 + S1 文档分类的结果，自动选择最优的解析引擎和参数配置。

职责：
1. 策略选择：基于分类结果 + 质量评估 → 选择引擎
2. Fallback 链：主引擎失败时自动降级到备选引擎
3. 引擎配置：统一管理各引擎的连接参数
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from mineru.utils.custom.doc_classifier import DocType
from mineru.utils.custom.doc_quality import DocumentQuality


@dataclass
class EngineRoute:
    """解析引擎路由结果。

    Attributes:
        doc_type: 文档分类结果。
        engine_backend: MinerU 后端字符串（如 "hybrid-auto-engine"）。
        strategy: 推荐策略描述。
        extra_kwargs: 传递给后端的额外参数。
        quality: 质量分析结果（可能为 None）。
        fallback_engine: 主引擎失败时的备选后端。
    """

    doc_type: DocType
    engine_backend: str
    strategy: str
    extra_kwargs: dict[str, Any] = field(default_factory=dict)
    quality: Optional[DocumentQuality] = None
    fallback_engine: Optional[str] = None


# ---------------------------------------------------------------------------
# 策略路由矩阵
# ---------------------------------------------------------------------------

# 针对不同文档类型的策略优先级：
# DOCUMENT_PARSE → 根据质量选择最合适的 MinerU 后端
# STRUCTURED_TABLE → Hybrid + OCR 补充（当前已实现）
# FORM_KVP → KVP Pipeline（外部 MLLM）

def select_engine_route(
    doc_type: DocType,
    quality: Optional[DocumentQuality] = None,
    user_backend: Optional[str] = None,
    user_kvp_engine: Optional[str] = None,
    user_kvp_server_url: Optional[str] = None,
    user_kvp_verify_engine: Optional[str] = None,
) -> EngineRoute:
    """根据分类结果和质量评估选择最优解析路径。

    Args:
        doc_type: 文档分类结果。
        quality: 质量分析结果（可选）。
        user_backend: 用户指定的 MinerU 后端（None 则自动选择）。
        user_kvp_engine: 用户指定的 KVP 引擎。
        user_kvp_server_url: 用户指定的 KVP 服务地址。
        user_kvp_verify_engine: VLM 纠错引擎（None 表示不纠错）。

    Returns:
        EngineRoute 对象，包含选定的引擎和后端配置。
    """
    if user_backend:
        # 用户显式指定了后端，跳过自动选择
        return EngineRoute(
            doc_type=doc_type,
            engine_backend=user_backend,
            strategy="user_override",
            quality=quality,
        )

    if doc_type == DocType.FORM_KVP:
        # 🔴 票据/卡证 → KVP Pipeline
        # 默认引擎优先级：用户指定 > 环境变量 KVP_ENGINE > pp-structure（离线优先）
        kvp_engine = (
            user_kvp_engine
            or os.environ.get("KVP_ENGINE")
            or "pp-structure"
        )
        kvp_url = user_kvp_server_url or os.environ.get("KVP_SERVER_URL") or None
        # VLM 纠错引擎：环境变量 KVP_VERIFY_ENGINE 可配置
        kvp_verify = (
            user_kvp_verify_engine
            or os.environ.get("KVP_VERIFY_ENGINE")
            or None
        )
        return EngineRoute(
            doc_type=doc_type,
            engine_backend="kvp",
            strategy=f"form_kvp:{kvp_engine}",
            extra_kwargs={
                "kvp_engine": kvp_engine,
                "kvp_server_url": kvp_url,
                "kvp_verify_engine": kvp_verify,
            },
            quality=quality,
            fallback_engine="hybrid-auto-engine",  # KVP 失败时回退
        )

    elif doc_type == DocType.STRUCTURED_TABLE:
        # 🟡 密集表格 → Hybrid + OCR 补充
        return EngineRoute(
            doc_type=doc_type,
            engine_backend="hybrid-auto-engine",
            strategy="structured_table",
            extra_kwargs={
                "parse_method": "ocr",  # 表格用 OCR 模式更精确
                "table_enable": True,
            },
            quality=quality,
        )

    else:
        # 🟢 通用文档 → 根据质量选择后端
        if quality and quality.ocr_difficulty == "high":
            # OCR 难度高时走 VLM 端到端
            return EngineRoute(
                doc_type=doc_type,
                engine_backend="vlm-auto-engine",
                strategy="general_vlm_high_quality",
                quality=quality,
            )
        elif quality and quality.ocr_difficulty == "medium":
            # 中等难度走 Hybrid
            return EngineRoute(
                doc_type=doc_type,
                engine_backend="hybrid-auto-engine",
                strategy="general_hybrid_medium_quality",
                quality=quality,
            )
        else:
            # 低难度可以用 pipeline 或 hybrid
            return EngineRoute(
                doc_type=doc_type,
                engine_backend="hybrid-auto-engine",
                strategy="general_default",
                quality=quality,
            )


# ---------------------------------------------------------------------------
# 路由决策摘要
# ---------------------------------------------------------------------------

def make_routing_summary(
    doc_type: DocType,
    route: EngineRoute,
    quality: Optional[DocumentQuality] = None,
) -> dict[str, Any]:
    """生成路由决策摘要，用于日志和 API 响应。

    Args:
        doc_type: 文档分类结果。
        route: 引擎路由结果。
        quality: 质量分析结果。

    Returns:
        包含完整决策信息的 dict。
    """
    summary: dict[str, Any] = {
        "doc_type": doc_type.value,
        "engine": route.engine_backend,
        "strategy": route.strategy,
    }

    if quality:
        summary["quality"] = {
            "score": round(quality.quality_score, 3),
            "ocr_difficulty": quality.ocr_difficulty,
            "has_stamp": quality.has_stamp,
            "is_blurry": quality.is_blurry,
            "dpi": quality.dpi,
            "page_count": quality.page_count,
        }

    if route.fallback_engine:
        summary["fallback_engine"] = route.fallback_engine

    logger.info(
        f"路由决策: {doc_type.value} → {route.engine_backend} "
        f"({route.strategy})"
    )
    return summary
