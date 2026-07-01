#!/bin/bash
# ============================================================
# MinerU Docker 构建脚本
# usage: bash build-docker.sh
#
# 模型通过 runtime volume 挂载，不入镜像（镜像更小，约 8GB）
# ============================================================
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-mineru:custom}"

echo "==> 1/2 构建镜像（不含模型，运行时挂载）..."
docker build -t "$IMAGE_NAME" .

echo "==> 2/2 完成!"
echo ""
echo "镜像:"
docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' "$IMAGE_NAME"
echo ""
echo "=== 启动（替换 MODEL_PATH 为你的实际模型路径）==="
echo ""
echo "  # 方式1: docker run"
echo "  docker run -d --gpus all -p 8000:8000 --name mineru-api --restart always \\"
echo "    -v /path/to/PDF-Extract-Kit-1.0:/models/pipeline:ro \\"
echo "    -v /path/to/MinerU2.5-Pro-2605-1.2B:/models/vlm:ro \\"
echo "    $IMAGE_NAME"
echo ""
echo "  # 方式2: docker compose"
echo "  MODEL_PIPELINE=/path/to/PDF-Extract-Kit-1.0 \\"
echo "  MODEL_VLM=/path/to/MinerU2.5-Pro-2605-1.2B \\"
echo "  docker compose up -d"
echo ""
echo "  # 方式3: 如果模型已放在项目 models/ 下"
echo "  docker compose up -d"
