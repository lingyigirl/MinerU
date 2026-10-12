# ============================================================
# MinerU fork（develop）海光 BW1000（深算三号）镜像 —— 方式 A（薄层 COPY）
#
# 把 fork 的 mineru 源码烤进 BW1000 DCU 基础镜像，只覆盖已安装的
# 上游 mineru 包，不动 torch/numpy/opencv/DTK（避免破坏 DCU 加速）。
#
# ⚠️ FROM 的 mineru:bw1000-vllm-latest 必须是「重新构建、mineru 3.4.4」的
#    基础镜像，由 docker/china/bw1000.Dockerfile 生成（联网装 mineru 3.4.4 + 模型）：
#       docker build --network=host -t mineru:bw1000-vllm-latest \
#         -f docker/china/bw1000.Dockerfile .
#
# 构建（在仓库根目录执行，或直接跑 build-bw1000.sh）:
#   docker build -f offline-package-bw1000/bw1000-fork.Dockerfile \
#     -t mineru:bw1000-fork-3.4.4 .
# ============================================================

FROM mineru:bw1000-vllm-latest

# ---- 修复 DCU torch 的 numpy 兼容（关键）----
# 基础镜像的通用 pip 依赖行把 numpy 装成了 2.x，而 DCU 版 torch 2.5.1
# 是针对 numpy 1.x 编译的；numpy 2.x 下 import torch 在 torch/export 处
# 段错误崩溃。这里降级 numpy<2，已在基础镜像 docker/china/bw1000.Dockerfile
# 同步钉死（重建基础镜像时生效）。此层同时保证现有基础镜像可直接复用。
RUN python3 -m pip install --no-cache-dir "numpy<2" -i https://mirrors.aliyun.com/pypi/simple && \
    python3 -m pip cache purge

# ---- 薄层 COPY：只覆盖已安装的上游 mineru 源码，不动依赖 ----
# 用 importlib.util.find_spec 定位 mineru 路径（【不执行模块】——
# 上游 mineru 3.4.4 的 types.py import 了未声明的 docvortex，
# 直接 import 会 ModuleNotFoundError，故不能 import mineru 取 __file__）。
# fork 全仓不引用 docvortex（types.py/__init__.py 为空壳），覆盖后无需该依赖。
COPY mineru/ /opt/mineru-fork/
RUN python3 -c "\
import importlib.util, os, shutil; \
spec = importlib.util.find_spec('mineru'); \
p = os.path.dirname(spec.origin) if spec.origin else list(spec.submodule_search_locations)[0]; \
shutil.rmtree(p); \
shutil.copytree('/opt/mineru-fork', p); \
" && rm -rf /opt/mineru-fork

# ENTRYPOINT 由基础镜像继承（已 export MINERU_MODEL_SOURCE=local）
