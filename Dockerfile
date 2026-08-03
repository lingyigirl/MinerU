# ============================================================
# MinerU 3.4.4 Custom — GPU 服务器 FastAPI 部署
# 模型打包进镜像，开箱即用
#
# 构建: docker build -t mineru:custom .
# 启动: docker run --gpus all -p 8000:8000 mineru:custom
#
# 镜像大小约 13-14 GB（vllm 基镜像 + 依赖 + 模型 4.3GB）
# ============================================================

FROM vllm/vllm-openai:v0.11.2

# ---- 系统依赖（中文字体 + OpenCV） ----
RUN apt-get update && \
    apt-get install -y \
        fonts-noto-core \
        fonts-noto-cjk \
        fontconfig \
        libgl1 \
        curl && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ---- 设置工作目录 ----
WORKDIR /opt/mineru

# ---- 先拷贝依赖文件和源码（version.py 供 pyproject.toml 动态读取版本号） ----
COPY pyproject.toml README.md ./
COPY mineru/ ./mineru/

# ---- 安装核心依赖 ----
RUN python3 -m pip install --no-cache-dir -e ".[core]" --break-system-packages && \
    python3 -m pip install --no-cache-dir "pdftext<0.7.0" --break-system-packages && \
    python3 -m pip cache purge

# ---- 配置文件（模型通过运行时挂载） ----
RUN printf '{\n\
  "models-dir": {\n\
    "pipeline": "/models/pipeline",\n\
    "vlm": "/models/vlm"\n\
  }\n\
}\n' > /root/mineru.json

# ---- 环境变量 ----
ENV MINERU_MODEL_SOURCE=local
ENV MINERU_DEVICE_MODE=cuda
ENV MINERU_VLM_FORMULA_ENABLE=true
ENV MINERU_VLM_TABLE_ENABLE=true
ENV MINERU_PROCESSING_WINDOW_SIZE=64

# ---- 暴露端口 ----
EXPOSE 8000

# ---- 健康检查 ----
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ---- 启动 FastAPI 服务 ----
ENTRYPOINT ["mineru-api"]
