# ============================================================
# MinerU fork（develop）海光 BW1000 基础镜像（DCU）
#
# 从海光 DCU 基础镜像出发，安装 mineru 及其依赖。模型通过
# --build-context 烤入（复用构建机已下载的本地模型，避免在
# 镜像构建时联网下载 ~34GB 模型）。
#
# ⚠️ 兼容性关键决策（已确认）：
#   基座镜像自带 torch 2.5.1+das.dtk25042 + vllm 0.9.2+das.dtk25042
#   （DCU 适配版，配套 DTK 25.04）。mineru 3.4.4 元数据要求
#   torch>=2.6.0 / vllm>=0.10.1.1，但【不可升级】——PyPI 是 CUDA 版，
#   会破坏 DCU 适配 vllm 且看不到 BW1000 卡。
#   因此本文件用 NO-DEPS 装 mineru 包本体，显式装非 torch 依赖，
#   冻结 torch/torchvision/vllm 为 DCU 版。transformers 路线 VLM/hybrid
#   可用；vllm 路线（vlm-vllm-async-engine）不用。
#
# 构建：
#   docker buildx build --load \
#     --build-context "models=/zhangbo/mineru_models" \
#     -t mineru:bw1000-vllm-latest \
#     -f docker/china/bw1000.Dockerfile .
#
# ⚠️ 基座镜像来自源申 Harbor（harbor.sourcefind.cn，海光 DCU 官方基座，
#    amd64/x86_64 + BW1000 需 rock-6.3.8+ 驱动 + DTK 25.04，驱动在宿主机）。
# ⚠️ 首次构建需联网（pip 拉 mineru 源码 + 非 torch 依赖）。
# ============================================================

FROM harbor.sourcefind.cn:5443/dcu/admin/base/vllm:0.9.2-ubuntu22.04-dtk25.04.2-1226-das1.7-py3.10-20251226

# 安装中文字体 + OpenCV 依赖
RUN apt-get update && \
    apt-get install -y \
        fonts-noto-core \
        fonts-noto-cjk \
        fontconfig \
        libgl1 \
        curl \
        git && \
    fc-cache -fv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ---- 安装 mineru【不升级 torch/vllm】----
# 1) NO-DEPS 安装 mineru 包本体（含 entry point / dist-info），不改任何已装包
# 2) 显式安装 mineru[core] 的非 torch 依赖；transformers/accelerate 基座已满足
# 3) torch/torchvision/vllm/flash_attn 全部冻结为基座 DCU 适配版，绝不触碰
RUN python3 -m pip install -U pip -i https://mirrors.aliyun.com/pypi/simple && \
    python3 -m pip install --no-cache-dir --no-deps \
        'mineru[core]>=3.0.0' \
        -i https://mirrors.aliyun.com/pypi/simple && \
    python3 -m pip install --no-cache-dir \
        click loguru "numpy<2" tqdm requests httpx pillow pypdfium2 pypdf \
        reportlab pdftext modelscope huggingface-hub json-repair opencv-python \
        fast-langdetect openai beautifulsoup4 "magika<1.1,>=0.6" \
        mineru-vl-utils python-docx pypptx-with-oxml mammoth pylatexenc lxml \
        openpyxl fastapi python-multipart uvicorn \
        PyYAML ftfy "shapely>=2.0.7,<3" pyclipper safetensors onnxruntime \
        transformers accelerate \
        -i https://mirrors.aliyun.com/pypi/simple && \
    python3 -m pip cache purge

# 校验：torch/torchvision/vllm 版本必须保持基座 DCU 版（未被替换）
RUN python3 -c "\
import torch, vllm, importlib.metadata as md; \
t = md.version('torch'); v = md.version('vllm'); \
print('torch', t); print('vllm', v); \
assert 'das' in t and 'das' in v, 'DCU 适配包被替换，构建中止'; \
"

# ---- 烤入本地模型（构建时通过 --build-context models=... 传入）----
# 本地模型目录 /zhangbo/mineru_models/ 是 modelscope hub 缓存格式：
#   hub/models/OpenDataLab/PDF-Extract-Kit-1___0（Pipeline + OriCls）
#   hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B（VLM）
# 一并 COPY 到镜像内的 modelscope 缓存路径，全程无需联网下载模型。
COPY --from=models hub/ /root/.cache/modelscope/hub/

# ---- 本地 mineru.json —— 指向镜像内模型路径（MINERU_MODEL_SOURCE=local 时读取）----
RUN printf '{\n\
  "models-dir": {\n\
    "pipeline": "/root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",\n\
    "vlm": "/root/.cache/modelscope/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"\n\
  },\n\
  "config_version": "1.3.2"\n\
}\n' > /root/mineru.json

# 设置环境变量
ENV MINERU_MODEL_SOURCE=local

# 默认入口
ENTRYPOINT ["/bin/bash", "-c", "export MINERU_MODEL_SOURCE=local && exec \"$@\"", "--"]