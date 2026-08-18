#!/bin/bash
# ============================================================
# 构建 fork 版 PPU 镜像并导出 tar.gz（方式 A）
#
# 用法（在仓库根目录执行）:
#   bash offline-package-ppu/build-ppu-fork.sh
#
# 前置：本机已有「重新构建、mineru 3.4.4」的基础镜像 mineru:ppu-vllm-latest
#       （由 docker/china/ppu.Dockerfile 生成，联网装 mineru 3.4.4 + 模型）:
#         docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .
#       不能用服务器旧镜像（如 2 月 mineru0210.tar）—— 旧镜像缺 3.4.4 依赖。
# 产物：offline-package-ppu/mineru-ppu-fork-3.4.4.tar.gz
# ============================================================
set -euo pipefail

BASE_IMAGE="${BASE_IMAGE:-mineru:ppu-vllm-latest}"
IMAGE_NAME="${IMAGE_NAME:-mineru:ppu-fork-3.4.4}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/mineru-ppu-fork-3.4.4.tar.gz"

# ---- 前置检查：基础镜像必须存在且是 3.4.4 ----
if ! docker image inspect "${BASE_IMAGE}" &> /dev/null; then
    echo "❌ 基础镜像 ${BASE_IMAGE} 不存在。"
    echo ""
    echo "   方式 A 需要「重新构建、mineru 3.4.4」的基础镜像，请先在构建机联网重建："
    echo "     docker build --network=host -t ${BASE_IMAGE} -f docker/china/ppu.Dockerfile ."
    echo ""
    echo "   ⚠️ 不要用服务器旧镜像（如 2 月 mineru0210.tar）做 FROM："
    echo "     薄层 COPY 只换代码不换依赖，旧镜像缺 pdftext/magika/mineru-vl-utils。"
    exit 1
fi

echo "==> 构建镜像 ${IMAGE_NAME}（context: ${REPO_ROOT}）"
docker build -f "${SCRIPT_DIR}/ppu-fork.Dockerfile" -t "${IMAGE_NAME}" "${REPO_ROOT}"

echo "==> 导出镜像到 ${OUTPUT_FILE}"
docker save "${IMAGE_NAME}" -o "${OUTPUT_FILE}"

echo "==> 完成"
ls -lh "${OUTPUT_FILE}"
