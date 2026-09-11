# ============================================================
# MinerU fork（develop）PPU 镜像 —— 方式 A（薄层 COPY）
#
# 把 fork 的 mineru 源码烤进 PPU 基础镜像，只覆盖已安装的
# 上游 mineru 包，不动 torch/numpy/opencv（避免破坏 PPU 加速）。
#
# ⚠️ FROM 的 mineru:ppu-vllm-latest 必须是「重新构建、mineru 3.4.4」的
#    基础镜像，由 docker/china/ppu.Dockerfile 生成（联网装 mineru 3.4.4 + 模型）：
#      docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .
#    不能用服务器旧镜像（如 2 月 mineru0210.tar）做 FROM —— 薄层 COPY
#    只换代码不换依赖，旧镜像缺 pdftext/magika/mineru-vl-utils，
#    烤出来仍会 import 报错（和方式 B 同一个坑）。
#
# 构建（在仓库根目录执行，或直接跑 build-ppu-fork.sh）:
#   docker build -f offline-package-ppu/ppu-fork.Dockerfile -t mineru:ppu-fork-3.4.4 .
# ============================================================

FROM mineru:ppu-vllm-latest

# 0. 补 OriCls 模型（基础镜像 2026-09-08 前构建时下载列表漏了该模型，
#    见 docker/china/ppu.Dockerfile:29-37 的修复说明。此处从本地模型文件补齐，
#    避免依赖 mineru-models-download）。
COPY offline-package-ppu/oricls/ \
     /root/.cache/modelscope/models/OpenDataLab--PDF-Extract-Kit-1.0/snapshots/master/models/OriCls/

# 薄层 COPY：只覆盖已安装的上游 mineru 源码，不动依赖。
# 目标路径用运行时解析（避免猜 site-packages / dist-packages），
# 保留基础镜像的 .dist-info 与 mineru-api 等 entry point 不变。
COPY mineru/ /opt/mineru-fork/
RUN python3 -c "import mineru,os,shutil;p=os.path.dirname(mineru.__file__);shutil.rmtree(p);shutil.copytree('/opt/mineru-fork',p)" && \
    rm -rf /opt/mineru-fork

# ENTRYPOINT 由基础镜像继承（已 export MINERU_MODEL_SOURCE=local）
