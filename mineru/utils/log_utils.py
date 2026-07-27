#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# [自定义] 从 RAGFlow 适配，用于统一管理 MinerU 的 loguru 日志配置

import logging
import logging.handlers
import os
import os.path
import sys
import time
import warnings
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from loguru import logger as _loguru_logger

initialized_root_logger = False

# 默认日志格式: 时间 | 级别 | 进程ID | 模块:函数:行号 - 消息
DEFAULT_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level:<8} | "
    "{process} | "
    "{name}:{function}:{line} - {message}"
)

# 默认屏蔽噪声包（常规操作日志量大的第三方库）
_DEFAULT_NOISY_PACKAGES = [
    "peewee",
    "pdfminer",
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "httpx",
    "httpcore",
    "asyncio",
    "boto3",
    "botocore",
    "s3transfer",
    "elastic_transport",
    "opensearch",
]


class InterceptHandler(logging.Handler):
    """将标准库 logging 的日志记录转发到 Loguru。

    用于统一处理第三方库（peewee, pdfminer 等）的日志输出，
    使其与项目日志合并到同一个日志文件和格式。
    """

    def emit(self, record: logging.LogRecord) -> None:
        """处理一条标准库 logging 日志记录。

        通过回溯调用栈找到真正的日志发起位置，
        确保 Loguru 记录的 ``{name}:{function}:{line}`` 指向原始调用者。

        Args:
            record: 标准库 logging 日志记录对象。
        """
        # 将 stdlib 日志级别映射为 Loguru 级别名称
        try:
            level = _loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 回溯调用栈，跳过 log_utils.py 自身和 logging 模块内部帧
        frame = logging.currentframe()
        depth = 0
        while frame:
            filename = frame.f_code.co_filename
            if filename in (logging.__file__, __file__):
                frame = frame.f_back
                depth += 1
            else:
                break

        _loguru_logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def init_root_logger(
    logfile_basename: str,
    log_format: str | None = None,
    log_dir: str | None = None,
    console_sink: Any = None,
) -> None:
    """初始化日志系统。

    使用 Loguru 作为日志后端，配置控制台和文件双输出。
    文件日志按每日轮转，保留 7 天，自动 zip 压缩。
    日志写入使用 enqueue 模式，确保异步安全。

    Args:
        logfile_basename: 日志文件基础名称。例如 ``"mineru_api"`` 生成
            ``{log_dir}/logs/mineru_api.log``。
        log_format: 自定义 loguru 格式字符串。为 None 时使用
            :data:`DEFAULT_LOG_FORMAT`（含模块名/函数名/行号）。
        log_dir: 日志文件存放目录。为 None 时读取
            ``MINERU_LOG_DIR`` 环境变量，默认为 ``~/mineru``。
        console_sink: 自定义控制台输出 sink。为 None 时使用
            ``sys.stderr``。CLI 模式传入自定义 sink 以支持进度条渲染。

    Returns:
        None
    """
    global initialized_root_logger
    if initialized_root_logger:
        return
    initialized_root_logger = True

    # ---------- 日志格式 ----------
    fmt = log_format if log_format is not None else DEFAULT_LOG_FORMAT

    # ---------- 日志文件路径 ----------
    if log_dir is None:
        log_dir = os.environ.get("MINERU_LOG_DIR", os.path.expanduser("~/mineru"))
    log_path = os.path.abspath(
        os.path.join(log_dir, "logs", f"{logfile_basename}.log")
    )
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    # ---------- 解析 MINERU_LOG_LEVELS 环境变量 ----------
    LOG_LEVELS = os.environ.get("MINERU_LOG_LEVELS", "")
    pkg_levels: dict[str, str] = {}

    for pkg_name_level in LOG_LEVELS.split(","):
        terms = pkg_name_level.split("=")
        if len(terms) != 2:
            continue
        pkg_name, pkg_level = terms[0].strip(), terms[1].strip().upper()
        if pkg_name:
            pkg_levels[pkg_name] = pkg_level

    # 默认屏蔽噪声包
    for pkg_name in _DEFAULT_NOISY_PACKAGES:
        if pkg_name not in pkg_levels:
            pkg_levels[pkg_name] = "WARNING"

    # 根级别：MINERU_LOG_LEVELS 中 root=xxx 优先级高于 MINERU_LOG_LEVEL
    root_level = pkg_levels.pop(
        "root", os.environ.get("MINERU_LOG_LEVEL", "INFO")
    ).upper()

    # ---------- 移除 Loguru 默认 handler ----------
    _loguru_logger.remove()

    # ---------- 控制台输出 ----------
    _loguru_logger.add(
        console_sink if console_sink is not None else sys.stderr,
        level=root_level,
        format=fmt,
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    # ---------- 文件输出：每日轮转，保留 7 天，zip 压缩 ----------
    _loguru_logger.add(
        str(log_path),
        level=root_level,
        format=fmt,
        rotation="00:00",
        retention="7 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    # ---------- 标准库 logging → Loguru 桥接 ----------
    logging.basicConfig(
        handlers=[InterceptHandler()],
        level=0,  # 不做级别过滤，由 Loguru sink 控制日志级别
        force=True,
    )

    # ---------- 各包独立日志级别 ----------
    for pkg_name, pkg_level in sorted(pkg_levels.items()):
        if not pkg_name:
            continue
        stdlib_level = getattr(logging, pkg_level.upper(), logging.INFO)
        pkg_logger = logging.getLogger(pkg_name)
        pkg_logger.setLevel(stdlib_level)

    # ---------- 捕获 Python warnings ----------
    logging.captureWarnings(True)

    # ---------- 启动日志 ----------
    msg = (
        f"{logfile_basename} log path: {log_path}, "
        f"root level: {root_level}, pkg levels: {pkg_levels}"
    )
    _loguru_logger.info(msg)


def log_exception(e: Exception, *args) -> None:
    """记录异常并重新抛出。

    从日志中记录异常堆栈（含局部变量诊断），同时尝试提取附加上下文对象中
    的 ``text`` 属性记录到日志。始终重新抛出原始异常。

    Args:
        e: 异常对象。
        *args: 附加上下文对象，会尝试读取其 ``text`` 属性记录到日志。

    Raises:
        e: 始终重新抛出传入的原始异常。
    """
    _loguru_logger.opt(exception=e).error(str(e))
    for a in args:
        try:
            text = getattr(a, "text")
        except Exception:
            text = None
        if text is not None:
            _loguru_logger.error(text)
            raise Exception(text)
        _loguru_logger.error(str(a))
    raise e


P = ParamSpec("P")
R = TypeVar("R")


def log_time(
    description: str | None = None,
    level: str = "DEBUG",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """函数执行耗时记录装饰器。

    在函数返回/抛出异常后自动记录耗时，适用于调试慢接口/慢查询。

    Usage::

        @log_time()
        def slow_function():
            ...

        @log_time("user query")
        async def search(query):
            ...

    Args:
        description: 可读描述，会出现在日志中 ``{description} completed``。
            为 None 时使用函数限定名（``module.function_name``）。
        level: 日志级别，默认 ``"DEBUG"``。

    Returns:
        装饰器闭包。
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        label = description or f"{func.__module__}.{func.__qualname__}"

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            t0 = time.perf_counter()
            try:
                result = await func(*args, **kwargs)  # type: ignore[misc]
            finally:
                elapsed = time.perf_counter() - t0
                _loguru_logger.opt(depth=1).log(
                    level,
                    "{} completed | elapsed={:.3f}s",
                    label,
                    elapsed,
                )
            return result

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            t0 = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - t0
                _loguru_logger.opt(depth=1).log(
                    level,
                    "{} completed | elapsed={:.3f}s",
                    label,
                    elapsed,
                )
            return result

        import inspect

        if inspect.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorator
