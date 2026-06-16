# ============================================================
# MinerU 3.2.0 Custom — GPU 服务器 FastAPI 部署
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

# ---- 先拷贝依赖文件（利用 Docker 缓存层） ----
COPY pyproject.toml README.md ./

# ---- 安装核心依赖 ----
RUN python3 -m pip install --no-cache-dir -e ".[core]" --break-system-packages && \
    python3 -m pip install --no-cache-dir "mineru-vl-utils>=1.0.0" --break-system-packages && \
    python3 -m pip cache purge

# ---- 拷贝项目源码 ----
COPY mineru/ ./mineru/

# ---- 拷贝模型文件（打包进镜像） ----
# 本地缓存路径 → 镜像内路径
# Pipeline 模型（PDF-Extract-Kit-1.0）：布局、OCR、公式、表格、方向分类等
COPY models/pipeline/ /opt/models/pipeline/

# VLM 模型（MinerU2.5-Pro）：文档理解大模型
COPY models/vlm/ /opt/models/vlm/

# ---- 配置文件 ----
RUN printf '{\n\
  "models-dir": {\n\
    "pipeline": "/opt/models/pipeline",\n\
    "vlm": "/opt/models/vlm"\n\
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
ENTRYPOINT ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]
