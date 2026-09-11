# ============================================================
# MinerU NVIDIA 方式 A — 自包含镜像（代码 + 模型烤入）
#
# 与 offline-package-ppu/ppu-fork.Dockerfile 对应，
# 构建产出可直接 docker load 运行的完整镜像，无需额外挂载模型。
#
# 构建方式（由 build-nvidia.sh 调用）:
#   docker buildx build --load \
#     --build-context models=<本地模型目录> \
#     -t mineru:3.4.4-upstream \
#     -f offline-package/nvidia-fork.Dockerfile \
#     <仓库根目录>
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

# ---- 安装 fork 代码（与根 Dockerfile 相同） ----
WORKDIR /opt/mineru

COPY pyproject.toml README.md ./
COPY mineru/ ./mineru/

RUN python3 -m pip install --no-cache-dir -e ".[core]" --break-system-packages && \
    python3 -m pip install --no-cache-dir "pdftext<0.7.0" --break-system-packages && \
    python3 -m pip cache purge

# ---- 烤入模型（构建时通过 --build-context models=... 传入本地模型目录） ----
COPY --from=models . /data/mineru_models/

# ---- 本地 mineru.json —— 指向镜像内模型路径 ----
RUN printf '{\n\
  "models-dir": {\n\
    "pipeline": "/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",\n\
    "vlm": "/data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"\n\
  },\n\
  "config_version": "1.3.2"\n\
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