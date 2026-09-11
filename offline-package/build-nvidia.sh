#!/bin/bash
# ============================================================
# MinerU NVIDIA 方式 A 构建脚本 —— 自包含镜像
#
# 用法: bash offline-package/build-nvidia.sh
#
# 功能: 构建自包含 Docker 镜像（fork 代码 + 模型烤入镜像），
#       产出单个 tar.gz，传输到内网 NVIDIA 服务器即可部署。
#
# 对应: offline-package-ppu/build-ppu-fork.sh
#
# 前置:
#   - 本地有 /zhangbo/mineru_models/（含全部模型，含 OriCls）
#   - Docker >= 24（支持 --build-context）
#   - 磁盘可用 >= 50GB
#
# 产物: offline-package/mineru-3.4.4-upstream.tar.gz
# ============================================================
set -euo pipefail

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()   { echo -e "${GREEN}  OK $1${NC}"; }
warn() { echo -e "${YELLOW}  WARN $1${NC}"; }
fail() { echo -e "${RED}  FAIL $1${NC}"; exit 1; }

# ---- 配置 ----
IMAGE_NAME="${IMAGE_NAME:-mineru:3.4.4-upstream}"
MODEL_SOURCE="${MODEL_SOURCE:-/zhangbo/mineru_models}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/mineru-3.4.4-upstream.tar.gz"

echo ""
echo "========================================================"
echo "  MinerU NVIDIA 方式 A 自包含镜像构建"
echo "========================================================"
echo "  IMAGE:     ${IMAGE_NAME}"
echo "  MODELS:    ${MODEL_SOURCE}"
echo "  OUTPUT:    ${OUTPUT_FILE}"
echo ""

# ---- Step 1: 检查前置条件 ----
log "检查前置条件..."

command -v docker &>/dev/null || fail "Docker 未安装"
docker info &>/dev/null || fail "Docker daemon 未运行"

DOCKER_VER=$(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1 || echo "0.0.0")
DOCKER_MAJOR=$(echo "$DOCKER_VER" | cut -d. -f1)
if [ "$DOCKER_MAJOR" -lt 24 ] 2>/dev/null; then
    fail "Docker 版本过低: $DOCKER_VER（需要 >= 24.0）"
fi
ok "Docker $DOCKER_VER"

docker buildx version &>/dev/null || fail "buildx 不可用"
ok "buildx 可用"

# 模型目录
if [ -d "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" ] && \
   [ -d "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" ]; then
    ok "Pipeline 模型 $(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" | cut -f1)"
    ok "VLM 模型 $(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" | cut -f1)"
else
    fail "模型目录不完整"
fi

if [ -f "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx" ]; then
    ok "OriCls 模型存在"
else
    warn "OriCls 模型缺失"
fi

AVAIL_GB=$(df -k "$SCRIPT_DIR" | tail -1 | awk '{print $4/1024/1024}' || echo 50)
if [ "$(echo "${AVAIL_GB} < 50" | bc -l 2>/dev/null || echo 0)" -eq 1 ]; then
    warn "磁盘空间仅 ${AVAIL_GB}GB，建议 >= 50GB"
else
    ok "磁盘空间 ${AVAIL_GB}GB"
fi

# ---- Step 2: 构建 ----
echo ""
log "构建镜像 ${IMAGE_NAME} ..."
log "  Dockerfile: offline-package/nvidia-fork.Dockerfile"
log "  --build-context models=${MODEL_SOURCE}"

cd "${REPO_ROOT}"
docker buildx build --load \
    --build-context "models=${MODEL_SOURCE}" \
    -t "${IMAGE_NAME}" \
    -f offline-package/nvidia-fork.Dockerfile \
    .

# 验证
docker run --rm --entrypoint "" "${IMAGE_NAME}" \
    test -d /data/mineru_models || fail "镜像中未找到模型目录"
IMAGE_SIZE=$(docker images "${IMAGE_NAME}" --format '{{.Size}}')
ok "镜像构建完成: ${IMAGE_NAME} (${IMAGE_SIZE})"

# ---- Step 3: 导出 ----
echo ""
log "导出镜像到 ${OUTPUT_FILE} ..."
docker save "${IMAGE_NAME}" | gzip > "${OUTPUT_FILE}"
TAR_SIZE=$(ls -lh "${OUTPUT_FILE}" | awk '{print $5}')
ok "导出完成 (${TAR_SIZE})"

# ---- Step 4: 复制部署配置文件 ---
echo ""
log "复制部署配置文件..."
if [ -f "${REPO_ROOT}/deploy/check_server_env.sh" ]; then
    cp "${REPO_ROOT}/deploy/check_server_env.sh" "${SCRIPT_DIR}/"
    chmod +x "${SCRIPT_DIR}/check_server_env.sh"
    ok "check_server_env.sh 已复制"
fi
ok "compose-prod.yaml 和 README-internal-deploy.md 已在 offline-package/ 中"

# ---- Step 5: 生成物料清单 ----
echo ""
log "生成物料清单..."

BUILD_TIME=$(date '+%Y-%m-%d %H:%M:%S')
PIPELINE_SIZE=$(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" | cut -f1)
VLM_SIZE=$(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" | cut -f1)

cat > "${SCRIPT_DIR}/MANIFEST.txt" << EOF
==============================================================
  MinerU NVIDIA 方式 A 离线物料清单
  生成时间: ${BUILD_TIME}
  镜像: ${IMAGE_NAME}
  镜像大小: ${IMAGE_SIZE}（导出压缩后 ${TAR_SIZE}）
==============================================================

文件:
  - mineru-3.4.4-upstream.tar.gz    自包含镜像（代码 + 模型）
  - compose-prod.yaml               Docker Compose 启动配置
  - README-internal-deploy.md        部署操作指南
  - check_server_env.sh              环境检查脚本
  - MANIFEST.txt                      本文件

特点: 模型已烤入镜像（Pipeline ${PIPELINE_SIZE} + VLM ${VLM_SIZE}），无需挂载
=========================================================
EOF

ok "MANIFEST.txt 已生成"

# ---- 最终汇 ----
echo ""
echo "========================================================"
echo "  打包完成！"
echo "========================================================"
echo ""
echo "  输出: ${OUTPUT_FILE}"
echo "  大小: ${TAR_SIZE}"
echo ""
echo "部署:"
echo "  1. gunzip -c ${OUTPUT_FILE} | docker load"
echo "  2. 修改 compose-prod.yaml 中的 GPU 编号"
echo "  3. docker compose -f compose-prod.yaml up -d"
echo ""
