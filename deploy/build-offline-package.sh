#!/bin/bash
# ============================================================
# MinerU 3.4.4 离线部署物料一键打包脚本
#
# 用途: 从当前服务器打包 Docker 镜像 + 模型 + 配置文件，
#      产出可直接传输到内网离线服务器部署的完整物料包。
#
# 用法: bash deploy/build-offline-package.sh [输出目录]
#
# 示例:
#   bash deploy/build-offline-package.sh                    # 默认 ./offline-package/
#   bash deploy/build-offline-package.sh /data/mineru-pkg   # 指定输出目录
#
# 前置条件:
#   - Docker 已安装且 daemon 运行中
#   - 模型已下载到 /zhangbo/mineru_models/
#   - 当前在 MinerU 项目根目录
#   - 磁盘空间 ≥ 30GB
# ============================================================
set -euo pipefail

# ---- 配置 ----
IMAGE_NAME="${IMAGE_NAME:-mineru:3.4.4-upstream}"
MODEL_SOURCE="${MODEL_SOURCE:-/zhangbo/mineru_models}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${1:-${PROJECT_ROOT}/offline-package}"
FORCE_BUILD="${FORCE_BUILD:-0}"     # 1=强制重建镜像（只改 mineru/ 源码时 Dockerfile/pyproject 时间不变，需手动强制）

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"; }
ok()   { echo -e "${GREEN}  ✅ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠️  $1${NC}"; }
fail() { echo -e "${RED}  ❌ $1${NC}"; exit 1; }

# ---- 检查前置条件 ----
echo ""
echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}  MinerU 3.4.4 离线部署物料打包${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""
log "检查前置条件..."

# Docker
command -v docker &>/dev/null || fail "Docker 未安装"
docker info &>/dev/null || fail "Docker daemon 未运行"
ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"

# 模型目录
if [ -d "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" ] && \
   [ -d "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" ]; then
    PIPELINE_SIZE=$(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/PDF-Extract-Kit-1___0" 2>/dev/null | cut -f1)
    VLM_SIZE=$(du -sh "${MODEL_SOURCE}/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B" 2>/dev/null | cut -f1)
    ok "Pipeline 模型存在 (${PIPELINE_SIZE})"
    ok "VLM 模型存在 (${VLM_SIZE})"
else
    fail "模型目录不完整，请先下载模型: mineru-models-download -s modelscope -m all"
fi

# 磁盘空间
AVAIL_GB=$(df -k "${OUTPUT_DIR}" 2>/dev/null | tail -1 | awk '{print $4/1024/1024}' || df -k "${PROJECT_ROOT}" | tail -1 | awk '{print $4/1024/1024}')
if [ "$(echo "${AVAIL_GB} < 30" | bc -l 2>/dev/null || echo 0)" -eq 1 ]; then
    warn "磁盘可用空间仅 ${AVAIL_GB}GB，建议 ≥ 30GB"
else
    ok "磁盘可用空间: ${AVAIL_GB}GB"
fi

# ---- Step 1: 构建 Docker 镜像 ----
echo ""
log "Step 1/4: 构建 Docker 镜像 (${IMAGE_NAME})..."

cd "${PROJECT_ROOT}"

# 检查是否需要重建
NEED_BUILD=true
if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${IMAGE_NAME}$"; then
    # 镜像已存在，检查 Dockerfile 和 pyproject.toml 是否比镜像新
    IMAGE_DATE=$(docker inspect "${IMAGE_NAME}" --format '{{.Created}}' 2>/dev/null | cut -dT -f1)
    DOCKERFILE_DATE=$(git log -1 --format="%ai" -- Dockerfile 2>/dev/null | cut -d' ' -f1)
    PYPROJECT_DATE=$(git log -1 --format="%ai" -- pyproject.toml 2>/dev/null | cut -d' ' -f1)

    if [ "${IMAGE_DATE}" \> "${DOCKERFILE_DATE}" ] 2>/dev/null && \
       [ "${IMAGE_DATE}" \> "${PYPROJECT_DATE}" ] 2>/dev/null; then
        log "镜像已是最新 (${IMAGE_DATE})，跳过构建"
        NEED_BUILD=false
    else
        log "源文件有更新，需要重建镜像"
    fi
fi

# 强制重建开关
if [ "${FORCE_BUILD}" = "1" ]; then
    log "FORCE_BUILD=1，强制重建镜像"
    NEED_BUILD=true
fi

if $NEED_BUILD; then
    log "开始构建（预计 10-15 分钟）..."
    IMAGE_NAME="${IMAGE_NAME}" bash build-docker.sh
    ok "镜像构建完成"
else
    ok "镜像复用已有"
fi

# ---- Step 2: 导出 Docker 镜像 ----
echo ""
log "Step 2/4: 导出 Docker 镜像..."

mkdir -p "${OUTPUT_DIR}"

IMAGE_TAR="${OUTPUT_DIR}/mineru-3.4.4-upstream.tar.gz"
log "导出 ${IMAGE_NAME} → ${IMAGE_TAR}（预计 3-5 分钟）..."
docker save "${IMAGE_NAME}" | gzip > "${IMAGE_TAR}"

IMAGE_SIZE=$(ls -lh "${IMAGE_TAR}" | awk '{print $5}')
ok "镜像导出完成 (${IMAGE_SIZE})"

# ---- Step 3: 打包模型文件 ----
echo ""
log "Step 3/4: 打包模型文件..."

MODELS_TAR="${OUTPUT_DIR}/mineru-models-3.4.4.tar.gz"
log "打包 ${MODEL_SOURCE} → ${MODELS_TAR}（预计 2-3 分钟）..."
tar czf "${MODELS_TAR}" -C "${MODEL_SOURCE}" .

MODELS_SIZE=$(ls -lh "${MODELS_TAR}" | awk '{print $5}')
ok "模型打包完成 (${MODELS_SIZE})"

# ---- Step 4: 汇总部署文件 ----
echo ""
log "Step 4/4: 汇总部署配置文件..."

# 创建 mineru.json 模板（适配内网路径）
cat > "${OUTPUT_DIR}/mineru-prod-template.json" << 'JSONEOF'
{
    "bucket_info": {
        "bucket-name-1": ["ak", "sk", "endpoint"]
    },
    "latex-delimiter-config": {
        "display": { "left": "$$", "right": "$$" },
        "inline": { "left": "$", "right": "$" }
    },
    "llm-aided-config": {
        "title_aided": {
            "api_key": "your_api_key",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-plus",
            "enable_thinking": false,
            "enable": false
        }
    },
    "models-dir": {
        "pipeline": "/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",
        "vlm": "/data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"
    },
    "config_version": "1.3.2"
}
JSONEOF
ok "mineru.json 模板已生成"

cp "${PROJECT_ROOT}/deploy/compose-prod.yaml" "${OUTPUT_DIR}/"
ok "compose-prod.yaml 已复制"

cp "${PROJECT_ROOT}/deploy/check_server_env.sh" "${OUTPUT_DIR}/"
chmod +x "${OUTPUT_DIR}/check_server_env.sh"
ok "check_server_env.sh 已复制"

cp "${PROJECT_ROOT}/deploy/README-internal-deploy.md" "${OUTPUT_DIR}/" 2>/dev/null || true
if [ -f "${OUTPUT_DIR}/README-internal-deploy.md" ]; then
    ok "README-internal-deploy.md 已复制"
fi

# ---- 生成文件清单 ----
echo ""
log "生成文件清单..."
cat > "${OUTPUT_DIR}/MANIFEST.txt" << EOF
================================================================
  MinerU 3.4.4 离线部署物料清单
  生成时间: $(date '+%Y-%m-%d %H:%M:%S')
  镜像 TAG: ${IMAGE_NAME}
================================================================

| 文件名                           | 大小      | 用途                        |
|---------------------------------|-----------|----------------------------|
| mineru-3.4.4-upstream.tar.gz   | ${IMAGE_SIZE}    | Docker 镜像                  |
| mineru-models-3.4.4.tar.gz     | ${MODELS_SIZE}    | Pipeline + VLM 模型         |
| mineru-prod-template.json       | -         | 运行时配置模板（需修改路径） |
| compose-prod.yaml               | -         | Docker Compose 启动配置      |
| check_server_env.sh             | -         | 内网服务器环境检查脚本       |
| README-internal-deploy.md       | -         | 内网部署操作指南             |
| MANIFEST.txt                    | -         | 本文件                      |

部署步骤:
  1. 将整个 offline-package/ 目录传输到内网服务器
  2. 在内网服务器上阅读 README-internal-deploy.md
  3. 按步骤部署

EOF

ok "文件清单已生成"

# ---- 最终汇总 ----
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  🎉 打包完成！${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "  输出目录: ${OUTPUT_DIR}"
echo ""
ls -lh "${OUTPUT_DIR}"/
echo ""
echo "  传输到内网服务器:"
echo "    scp -r ${OUTPUT_DIR} root@内网服务器IP:/data/"
echo ""
echo "  内网服务器部署:"
echo "    1. cd /data/offline-package"
echo "    2. bash check_server_env.sh    # 检查环境"
echo "    3. 阅读 README-internal-deploy.md"
