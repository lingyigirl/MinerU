"""custom/ 目录集中配置模块。

为 custom/ 下的本地 R&D 代码提供统一的外部配置管理。
优先级链（从高到低）：
  1. 环境变量（docker/K8s 场景优先）
  2. JSON 配置文件（持久化配置）
  3. 代码内默认值（兜底）

配置文件搜索路径（同 config_reader.py 的定位逻辑）：
  - 环境变量 MINERU_TOOLS_CONFIG_JSON 指定的绝对路径，或
  - ~/mineru.json（默认）

JSON 文件中的配置项：
  - 位于 "custom" 嵌套层下（推荐），如 {"custom": {"table_ocr_min_confidence": 0.9, ...}}
  - 或顶层直接以 custom_ / kvp_ 开头的键（兼容旧布局）
"""

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

# ── 配置文件搜索 ──────────────────────────────────────────────

_CONFIG_FILE_ENV = "MINERU_TOOLS_CONFIG_JSON"
_DEFAULT_CONFIG_FILE = "mineru.json"


def _locate_config_file() -> Optional[Path]:
    """定位 JSON 配置文件，复用 config_reader.py 的搜索模式。"""
    file_name = os.getenv(_CONFIG_FILE_ENV, _DEFAULT_CONFIG_FILE)
    if os.path.isabs(file_name):
        path = Path(file_name)
    else:
        path = Path.home() / file_name
    return path if path.exists() else None


# ── 配置缓存（模块级惰性加载） ───────────────────────────────

_CUSTOM_CFG: dict[str, Any] = {}


def _get_custom_config() -> dict[str, Any]:
    """惰性加载 JSON 配置文件中 custom 层的键值对。

    JSON 文件格式示例：
        {
            "custom": {
                "table_ocr_min_confidence": 0.9,
                "infer_missing_table_values": false,
                "kvp_engine": "pp-structure",
                ...
            },
            ...  // 其它 mineru.json 原有字段
        }

    兼容旧格式：顶层直接以 custom_ / kvp_ 开头的 key 也纳入。
    """
    if _CUSTOM_CFG:
        return _CUSTOM_CFG

    config_path = _locate_config_file()
    if config_path is None:
        _CUSTOM_CFG.clear()
        return _CUSTOM_CFG

    try:
        with open(config_path, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"配置文件 {config_path} 读取失败: {exc}")
        _CUSTOM_CFG.clear()
        return _CUSTOM_CFG

    # 优先用 custom 嵌套层，回退顶层 key
    section = raw.get("custom", {})
    if isinstance(section, dict):
        _CUSTOM_CFG.update(section)
    # Also pick up known-prefix top-level keys for backward compat
    for k, v in raw.items():
        if k.startswith("custom_") or k.startswith("kvp_"):
            _CUSTOM_CFG.setdefault(k, v)

    return _CUSTOM_CFG


# ── 通用配置读取器 ────────────────────────────────────────────

def _general_get(
    key: str,
    *,
    env_var: Optional[str] = None,
    default: Any = None,
    type_cast: Optional[Callable[[str], Any]] = None,
) -> Any:
    """统一配置读取：环境变量 > JSON 配置文件 > 默认值。

    Args:
        key: JSON 配置文件中的键名。
        env_var: 环境变量名（可选）。存在时优先级最高。
        default: 兜底默认值。
        type_cast: 类型转换函数（如 float, int 等），应用于来自环境变量或 JSON 的值。
    """
    # 1) 环境变量优先
    if env_var:
        env_val = os.getenv(env_var)
        if env_val is not None:
            try:
                return type_cast(env_val) if type_cast else env_val
            except (ValueError, TypeError):
                logger.warning(
                    f"环境变量 {env_var}={env_val} 转换失败，回退配置"
                )
    # 2) JSON 配置其次
    cfg = _get_custom_config()
    val = cfg.get(key)
    if val is not None:
        try:
            return type_cast(val) if type_cast else val
        except (ValueError, TypeError):
            pass
    # 3) 默认值兜底
    return default


def get_float(key: str, *, env_var: Optional[str] = None, default: float = 0.0) -> float:
    """读取浮点配置项。"""
    return _general_get(key, env_var=env_var, default=default, type_cast=float)


def get_int(key: str, *, env_var: Optional[str] = None, default: int = 0) -> int:
    """读取整数配置项。"""
    return _general_get(key, env_var=env_var, default=default, type_cast=int)


def get_bool(key: str, *, env_var: Optional[str] = None, default: bool = False) -> bool:
    """读取布尔配置项。"""
    return _general_get(key, env_var=env_var, default=default, type_cast=_to_bool)


def get_str(key: str, *, env_var: Optional[str] = None, default: str = "") -> str:
    """读取字串配置项。"""
    return _general_get(key, env_var=env_var, default=default, type_cast=str)


def _to_bool(val: Any) -> bool:
    """将各种格式转为布尔值。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return bool(val)


# ── 类型化 Getter（custom/ 模块专用） ──────────────────────────

# --- OCR 表格填充 ---

def get_table_ocr_min_confidence() -> float:
    """OCR 识别置信度门槛（守卫 10）。

    环境变量：MINERU_TABLE_OCR_MIN_CONFIDENCE
    JSON 键：  custom.table_ocr_min_confidence
    默认值：  0.8（设为 0 关闭过滤）
    """
    return get_float(
        "table_ocr_min_confidence",
        env_var="MINERU_TABLE_OCR_MIN_CONFIDENCE",
        default=0.8,
    )


def get_infer_missing_table_values() -> bool:
    """是否启用发票税率推断填充。

    环境变量：MINERU_INFER_MISSING_TABLE_VALUES
    JSON 键：  custom.infer_missing_table_values
    默认值：  False（推断值非识别结果，默认关闭）
    """
    return get_bool(
        "infer_missing_table_values",
        env_var="MINERU_INFER_MISSING_TABLE_VALUES",
        default=False,
    )


# --- span 字符游程救援 ---

def get_span_gap_rescue_enable() -> bool:
    """是否启用 span 字符游程救援（gap-aware char rescue）。

    原生文本层字符若中心点落在 span 框外，会被 fill_char_in_spans 直接丢弃 ——
    当 span 框（来自 VLM ocr_text 或 layout）被印章/图片块截断时，框外字符即丢失
    （表现为「：元」缺「单位」、「枣庄薛城」缺「支行」）。
    本开关启用第二遍救援：把紧邻 span 现有游程的未归属字符按间隙吸收进来。

    环境变量：MINERU_SPAN_GAP_RESCUE
    JSON 键：  custom.span_gap_rescue_enable
    默认值：  True
    """
    return get_bool(
        "span_gap_rescue_enable",
        env_var="MINERU_SPAN_GAP_RESCUE",
        default=True,
    )


def get_span_gap_rescue_ratio() -> float:
    """游程救援的最大水平间隙，按 span 高度的倍数计。

    间隙超过该倍数视为跨字段/跨列边界，停止吸收。
    中文正文的字间距远小于半个行高，而不同字段之间（如「支行」与「时间」间距 34.9pt
    对 11pt 行高）约为其 3 倍以上，故 0.5 能干净地切在字段边界。

    环境变量：MINERU_SPAN_GAP_RESCUE_RATIO
    JSON 键：  custom.span_gap_rescue_ratio
    默认值：  0.5
    """
    return get_float(
        "span_gap_rescue_ratio",
        env_var="MINERU_SPAN_GAP_RESCUE_RATIO",
        default=0.5,
    )


# --- 红色印章去除 ---

def get_seal_removal_enable() -> bool:
    """是否启用红色印章去除（页面图像预处理）。

    红色印章会被切成独立图片块，压在表头/字段文字上时导致两类错误：
    表头格被 VLM 误读（转入金额/借贷标志 → 借出金额）、字段 span 被图片块
    截断（「本方账号开户行」丢「支行」）。把印章红像素白化后两类错误同时消失。
    守卫规则见 mineru/utils/custom/seal_removal.py；只影响红色像素，黑印章不处理。

    环境变量：MINERU_SEAL_REMOVAL
    JSON 键：  custom.seal_removal_enable
    默认值：  True
    """
    return get_bool(
        "seal_removal_enable",
        env_var="MINERU_SEAL_REMOVAL",
        default=True,
    )


# --- 旋转几何判据 ---

def get_rotate_geom_enable() -> bool:
    """是否启用旋转修正的几何判据（pdf_utils.generate_rotation_corrected_pdf）。

    原判据只用「分类器置信度 >= theta」决定偏离多数派的页是否旋转，但真旋转页
    （实测 conf 0.4354-0.4481）与误判页（上限 0.439）的分布物理重叠，任何单
    阈值都无法分离（降 theta 是零和）。几何判据以「页面宽高比是否偏离文档多数派」
    决定【是否】旋转、分类器只决定【方向】，二者正交；且几何少数派集合与历史
    误判 180° 页集合（宽高比与多数派一致）不相交，不会复活旧的误转问题。

    环境变量：MINERU_ROTATE_GEOM_ENABLE
    JSON 键：  custom.rotate_geom_enable
    默认值：  True
    """
    return get_bool(
        "rotate_geom_enable",
        env_var="MINERU_ROTATE_GEOM_ENABLE",
        default=True,
    )


def get_rotate_geom_majority_min() -> float:
    """几何判据生效所需的文档多数派宽高比占比下限。

    占比低于该值的文档（横向/纵向接近 50:50）上几何多数派不可靠，判据整体
    失效、回退纯置信度逻辑（保守）。本批新发 96 页 93 横向 3 纵向，占比 0.97。

    环境变量：MINERU_ROTATE_GEOM_MAJORITY_MIN
    JSON 键：  custom.rotate_geom_majority_min
    默认值：  0.8
    """
    return get_float(
        "rotate_geom_majority_min",
        env_var="MINERU_ROTATE_GEOM_MAJORITY_MIN",
        default=0.8,
    )


def get_seal_removal_dilate_px() -> int:
    """印章守卫的膨胀核边长（像素），用于把被黑字切断的印章笔画并成整体。

    200 DPI 下默认 25px（约 3mm）足以跨越印章笔画间隙；调大可把相距更远的
    红色元素并作一个整体（更激进），调小则更保守。

    环境变量：MINERU_SEAL_REMOVAL_DILATE_PX
    JSON 键：  custom.seal_removal_dilate_px
    默认值：  25
    """
    return get_int(
        "seal_removal_dilate_px",
        env_var="MINERU_SEAL_REMOVAL_DILATE_PX",
        default=25,
    )


def get_seal_removal_min_area_ratio() -> float:
    """印章守卫的 bbox 面积占比下限。

    占比 = 连通域 bbox 面积 / 页面面积。低于下限视为小红色元素
    （红字金额、零星红点等），不白化。

    环境变量：MINERU_SEAL_REMOVAL_MIN_AREA_RATIO
    JSON 键：  custom.seal_removal_min_area_ratio
    默认值：  0.004
    """
    return get_float(
        "seal_removal_min_area_ratio",
        env_var="MINERU_SEAL_REMOVAL_MIN_AREA_RATIO",
        default=0.004,
    )


def get_seal_removal_max_area_ratio() -> float:
    """印章守卫的 bbox 面积占比上限。

    超过上限视为大片红色版面元素（整页红底、红色水印等），不白化，防误伤。

    环境变量：MINERU_SEAL_REMOVAL_MAX_AREA_RATIO
    JSON 键：  custom.seal_removal_max_area_ratio
    默认值：  0.08
    """
    return get_float(
        "seal_removal_max_area_ratio",
        env_var="MINERU_SEAL_REMOVAL_MAX_AREA_RATIO",
        default=0.08,
    )


# --- KVP 引擎 ---

def get_kvp_engine() -> str:
    """KVP 提取后端引擎名称。

    环境变量：KVP_ENGINE
    JSON 键：  custom.kvp_engine / 顶层 kvp_engine
    默认值：  "pp-structure"
    """
    return get_str("kvp_engine", env_var="KVP_ENGINE", default="pp-structure")


def get_kvp_server_url() -> Optional[str]:
    """KVP 服务 URL。

    环境变量：KVP_SERVER_URL
    JSON 键：  custom.kvp_server_url / 顶层 kvp_server_url
    """
    val = _general_get("kvp_server_url", env_var="KVP_SERVER_URL", default=None, type_cast=str)
    return val if val and val.strip() else None


def get_kvp_verify_engine() -> Optional[str]:
    """KVP VLM 纠错引擎名称。

    环境变量：KVP_VERIFY_ENGINE
    JSON 键：  custom.kvp_verify_engine / 顶层 kvp_verify_engine
    """
    val = _general_get("kvp_verify_engine", env_var="KVP_VERIFY_ENGINE", default=None, type_cast=str)
    return val if val and val.strip() else None


def get_kvp_api_key() -> Optional[str]:
    """KVP API 密钥。

    环境变量：KVP_API_KEY
    JSON 键：  custom.kvp_api_key / 顶层 kvp_api_key
    """
    return _general_get("kvp_api_key", env_var="KVP_API_KEY", default=None, type_cast=str)


def get_dashscope_api_key() -> Optional[str]:
    """阿里云 DashScope API 密钥（KVP 引擎备选）。

    环境变量：DASHSCOPE_API_KEY
    JSON 键：  custom.dashscope_api_key / 顶层 dashscope_api_key
    """
    return _general_get("dashscope_api_key", env_var="DASHSCOPE_API_KEY", default=None, type_cast=str)


__all__ = [
    # 通用 getter
    "get_float",
    "get_int",
    "get_bool",
    "get_str",
    # OCR 表格
    "get_table_ocr_min_confidence",
    "get_infer_missing_table_values",
    # span 游程救援
    "get_span_gap_rescue_enable",
    "get_span_gap_rescue_ratio",
    # 红色印章去除
    "get_seal_removal_enable",
    "get_seal_removal_dilate_px",
    "get_seal_removal_min_area_ratio",
    "get_seal_removal_max_area_ratio",
    # 旋转几何判据
    "get_rotate_geom_enable",
    "get_rotate_geom_majority_min",
    # KVP
    "get_kvp_engine",
    "get_kvp_server_url",
    "get_kvp_verify_engine",
    "get_kvp_api_key",
    "get_dashscope_api_key",
]
