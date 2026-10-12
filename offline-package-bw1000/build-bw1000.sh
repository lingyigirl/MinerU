#!/bin/bash
# ============================================================
# 构建 fork 版海光 BW1000 DCU 镜像并导出 tar.gz（方式 A）
#
# 用法（在仓库根目录执行）:
#   bash offline-package-bw1000/build-bw1000.sh
#
# 流程（两阶段，模型复用构建机本地已下载模型，非联网下载）:
#   1. 构建基础镜像 mineru:bw1000-vllm-latest
#      （docker/china/bw1000.Dockerfile，--build-context 烤入本地模型）
#   2. 薄层覆盖 fork 源码 → mineru:bw1000-fork-3.4.4
#   3. 导出 tar.gz
#
# 可配置:
#   MODEL_SOURCE - 本地模型目录（默认 /zhangbo/mineru_models，modelscope hub 缓存格式）
#   BASE_IMAGE   - 基础镜像 TAG（默认 mineru:bw1000-vllm-latest）
#   IMAGE_NAME   - 最终镜像 TAG（默认 mineru:bw1000-fork-3.4.4）
#
# 前置:
#   - 构建机有 Docker >= 24（支持 --build-context），首次需联网（拉基座 + pip 装依赖）
#   - 基座镜像 harbor.sourcefind.cn:5443/dcu/admin/base/vllm:...dtk25.04.2... 可拉取
#   - 本地模型目录完整（含 Pipeline + VLM + OriCls）
#   - 磁盘可用 >= 80GB
#
# 产物: offline-package-bw1000/mineru-bw1000-fork-3.4.4.tar.gz
# ============================================================
set -euo pipefail

MODEL_SOURCE="${MODEL_SOURCE:-/zhangbo/mineru_models}"
BASE_IMAGE="${BASE_IMAGE:-mineru:bw1000-vllm-latest}"
IMAGE_NAME="${IMAGE_NAME:-mineru:bw1000-fork-3.4.4}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/mineru-bw1000-fork-3.4.4.tar.gz"

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()   { echo -e "${GREEN}  OK $1${NC}"; }
warn() { echo -e "${YELLOW}  WARN $1${NC}"; }
fail() { echo -e "${RED}  FAIL $1${NC}"; exit 1; }

echo ""
echo "========================================================"
echo "  MinerU 海光 BW1000 方式 A 自包含镜像构建"
echo "  MODELS:    ${MODEL_SOURCE}"
echo "  BASE:      ${BASE_IMAGE}"
echo "  IMAGE:     ${IMAGE_NAME}"
echo "  OUTPUT:    ${OUTPUT_FILE}"
echo "========================================================"

# ---- Step 0: 前置检查 ----
log "检查前置条件..."

command -v docker &>/dev/null || fail "Docker 未安装"
docker info &>/dev/null || fail "Docker daemon 未运行"
DOCKER_MAJOR=$(docker --version | grep -oP '\d+\.\d+' | cut -d. -f1)
if [ "${DOCKER_MAJOR:-0}" -lt 24 ] 2>/dev/null; then
    fail "Docker 版本过低（需要 >= 24.0，以支持 --build-context）"
fi
docker buildx version &>/dev/null || fail "buildx 不可用"
ok "Docker $(docker --version | awk '{print $3}') + buildx"

# 本地模型目录
if [ -d "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" ] && \
   [ -d "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" ]; then
    PIPELINE_SIZE=$(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" 2>/dev/null | cut -f1)
    VLM_SIZE=$(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" 2>/dev/null | cut -f1)
    ok "Pipeline 模型 ${PIPELINE_SIZE}"
    ok "VLM 模型 ${VLM_SIZE}"
else
    fail "本地模型目录不完整（缺 PDF-Extract-Kit-1___0 或 MinerU2___5-Pro-2605-1___2B）"
fi
if [ -f "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx" ]; then
    ok "OriCls 模型存在"
else
    warn "OriCls 模型缺失（新增 fork 源码前需补齐，否则方向分类报错）"
fi

AVAIL_GB=$(df -k "$SCRIPT_DIR" 2>/dev/null | tail -1 | awk '{print int($4/1024/1024)}')
if [ "${AVAIL_GB:-0}" -lt 80 ]; then
    warn "磁盘空间仅 ${AVAIL_GB}GB，建议 >= 80GB"
else
    ok "磁盘空间 ${AVAIL_GB}GB"
fi

# ---- Step 1: 构建基础镜像（若已存在则复用） ----
echo ""
if docker image inspect "${BASE_IMAGE}" &> /dev/null; then
    ok "基础镜像 ${BASE_IMAGE} 已存在，跳过重建（如需重建：docker image rm ${BASE_IMAGE}）"
else
    log "构建基础镜像 ${BASE_IMAGE} ...（首次需联网拉基座 + pip 装依赖）"
    cd "${REPO_ROOT}"
    docker buildx build --load \
        --build-context "models=${MODEL_SOURCE}" \
        -t "${BASE_IMAGE}" \
        -f docker/china/bw1000.Dockerfile \
        .
    # 验证基础镜像内模型
    docker run --rm --entrypoint "" "${BASE_IMAGE}" \
        test -d /root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0 \
        && ok "基础镜像模型烤入成功" || fail "基础镜像内未找到模型目录"
fi
BASE_SIZE=$(docker images "${BASE_IMAGE}" --format '{{.Size}}')
ok "基础镜像: ${BASE_IMAGE} (${BASE_SIZE})"

# ---- Step 2: 构建 fork 覆盖镜像 ----
echo ""
log "构建 fork 镜像 ${IMAGE_NAME} ..."
cd "${REPO_ROOT}"
docker build \
    -f "${SCRIPT_DIR}/bw1000-fork.Dockerfile" \
    -t "${IMAGE_NAME}" \
    "${REPO_ROOT}"

# 验证 fork 源码 + 模型
docker run --rm --entrypoint "" "${IMAGE_NAME}" \
    python3 -c "import importlib.metadata as md; import mineru.utils.custom; print('fork 源码 ok v' + md.version('mineru'))"
docker run --rm --entrypoint "" "${IMAGE_NAME}" \
    test -d /root/.cache/modelscope/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B \
    && ok "fork 镜像模型完整" || fail "fork 镜像模型缺失"
IMAGE_SIZE=$(docker images "${IMAGE_NAME}" --format '{{.Size}}')
ok "镜像构建完成: ${IMAGE_NAME} (${IMAGE_SIZE})"

# ---- Step 3: 导出 tar.gz ----
echo ""
log "导出镜像到 ${OUTPUT_FILE} ..."
docker save "${IMAGE_NAME}" | gzip > "${OUTPUT_FILE}"
TAR_SIZE=$(ls -lh "${OUTPUT_FILE}" | awk '{print $5}')
ok "导出完成 (${TAR_SIZE})"

# ---- Step 4: 更新物料清单 ----
echo ""
log "更新物料清单..."
BUILD_TIME=$(date '+%Y-%m-%d %H:%M:%S')
cat > "${SCRIPT_DIR}/MANIFEST-bw1000.txt" << EOF
================================================================
  MinerU fork（develop）海光 BW1000 部署物料清单（方式 A）
  生成时间: ${BUILD_TIME}
  加速卡: 海光 BW1000（深算三号 DCU）/ 驱动 rock-6.3.8+ / DTK 25.04+
  镜像 TAG: ${IMAGE_NAME}
  镜像大小: ${IMAGE_SIZE}（导出压缩后 ${TAR_SIZE}）
================================================================

文件:
  - mineru-bw1000-fork-3.4.4.tar.gz    自包含镜像（fork 代码 + 模型烤入）
  - compose-bw1000.yaml                Docker Compose 启动配置
  - check_server_env.sh                环境检查脚本（部署前先跑）
  - README-bw1000-deploy.md            部署操作指南（部署人员读这个）
  - MANIFEST-bw1000.txt                本文件
  - (创建指令见 README-bw1000-build.md / 脚本 build-bw1000.sh)

镜像内模型:
  - Pipeline: ${PIPELINE_SIZE}
  - VLM: ${VLM_SIZE}
  - OriCls: 已包含

部署步骤:
  1. bash check_server_env.sh
  2. gunzip -c ${OUTPUT_FILE} | docker load
  3. 修改 compose-bw1000.yaml（DCU 卡号 + 路径）
  4. docker compose -f compose-bw1000.yaml up -d
================================================================
EOF
ok "MANIFEST-bw1000.txt 已更新"

# ---- 最终汇总 ----
echo ""
echo "========================================================"
echo "  打包完成！"
echo "========================================================"
echo "  输出: ${OUTPUT_FILE}"
echo "  大小: ${TAR_SIZE}"
echo ""
echo " 部署:"
echo "  1. gunzip -c ${OUTPUT_FILE} | docker load"
echo "  2. 修改 compose-bw1000.yaml 中的 DCU 卡号"
echo "  3. docker compose -f compose-bw1000.yaml up -d"
echo ""