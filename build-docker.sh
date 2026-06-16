#!/bin/bash
# ============================================================
# MinerU Docker 构建脚本（通用 Linux/Windows WSL/Mac）
#
# usage: bash build-docker.sh
#
# 前提: 已下载模型到本地缓存
#   mineru-models-download -s modelscope -m all
# ============================================================
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-mineru:custom}"
# 模型缓存路径（model_scope 默认）
MODEL_CACHE="${MODEL_CACHE:-$HOME/.cache/modelscope/hub/models/OpenDataLab}"

echo "==> 1/4 检测模型..."

rm -rf models 2>/dev/null || true
mkdir -p models

# Pipeline 模型
PIPELINE_DONE=false
for d in "$MODEL_CACHE/PDF-Extract-Kit-1.0" "$MODEL_CACHE/PDF-Extract-Kit-1___0"; do
    if [ -d "$d" ]; then
        cp -r "$d" models/pipeline
        PIPELINE_DONE=true
        break
    fi
done
if ! $PIPELINE_DONE; then
    echo "    [ERROR] Pipeline 模型未找到，请先下载: mineru-models-download -s modelscope -m all"
    exit 1
fi
echo "    Pipeline: $(du -sh models/pipeline 2>/dev/null | cut -f1)"

# VLM 模型（自动选择最新版本）
VLM_DONE=false
for vlm in "MinerU2.5-Pro-2604-1.2B" "MinerU2___5-Pro-2604-1___2B" "MinerU2.5-2509-1.2B" "MinerU2___5-2509-1___2B"; do
    if [ -d "$MODEL_CACHE/$vlm" ]; then
        cp -r "$MODEL_CACHE/$vlm" models/vlm
        VLM_DONE=true
        break
    fi
done
if ! $VLM_DONE; then
    echo "    [ERROR] VLM 模型未找到"
    exit 1
fi
echo "    VLM    : $(du -sh models/vlm 2>/dev/null | cut -f1)"

echo "==> 2/4 构建镜像 ($IMAGE_NAME)..."
docker build -t "$IMAGE_NAME" .

echo "==> 3/4 清理临时文件..."
rm -rf models/

echo "==> 4/4 完成!"
echo ""
echo "镜像:"
docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' "$IMAGE_NAME"
echo ""
echo "=== 启动 ==="
echo "  docker run --gpus all -p 8000:8000 $IMAGE_NAME"
echo ""
echo "=== 测试 ==="
echo "  curl -X POST http://localhost:8000/file_parse -F 'file=@test.pdf' -F 'backend=vlm-auto-engine'"
