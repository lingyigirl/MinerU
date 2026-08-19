#!/bin/bash
# ============================================================
# 打包 fork 源码（方式 B 用）→ mineru-src-3.4.4.tar.gz
#
# 用途：方式 B 源码挂载需要把 fork 的 mineru/ 包放到服务器上。
#       本脚本把当前仓库的 mineru/ 源码打成 tar.gz，随离线包分发，
#       服务器解压后即可作为 compose 的源码挂载源（无需 rsync 开发机）。
#
# 用法（任意目录执行，脚本自动定位仓库根）:
#   bash offline-package-ppu-mount/build-ppu-src.sh
#
# 产物：offline-package-ppu-mount/mineru-src-3.4.4.tar.gz
#   （含 mineru/ 包，排除 __pycache__/.git；gitignore 已忽略，不进 git）
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/mineru-src-3.4.4.tar.gz"

echo "==> 打包 fork 源码 mineru/ → ${OUTPUT_FILE}"
tar czf "${OUTPUT_FILE}" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.git' \
    -C "${REPO_ROOT}" mineru/

echo "==> 完成"
ls -lh "${OUTPUT_FILE}"
md5sum "${OUTPUT_FILE}"

echo ""
echo "服务器侧解压（得到 /data/mineru-src/mineru/，正好是 compose 挂载源）："
echo "  mkdir -p /data/mineru-src"
echo "  tar xzf mineru-src-3.4.4.tar.gz -C /data/mineru-src"
