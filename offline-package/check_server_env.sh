#!/bin/bash
# ============================================================
# MinerU 3.4.4 服务器环境检查脚本
#
# 用途：部署前检查服务器硬件和软件是否满足 MinerU 运行要求
# 用法：bash check_server_env.sh
#
# 检查项：
#   1. 服务器基本信息（品牌、虚拟化类型）
#   2. CPU（核心数、架构）
#   3. GPU（型号、显存、架构、CUDA 版本）
#   4. 内存（总量、可用量）
#   5. 磁盘（分区、剩余空间）
#   6. 操作系统（发行版、内核版本）
#   7. Python 环境
#   8. Docker 环境
#   9. NVIDIA 容器运行时
#  10. 网络连通性
# ============================================================
set -euo pipefail

# ---- 颜色定义 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # 无颜色

PASS="${GREEN}✅ PASS${NC}"
WARN="${YELLOW}⚠️  WARN${NC}"
FAIL="${RED}❌ FAIL${NC}"
INFO="${BLUE}ℹ️  INFO${NC}"

# ---- 阈值定义（来自 MinerU 官方文档） ----
MIN_CPU_CORES=8
MIN_RAM_GB=16
RECOMMENDED_RAM_GB=32
MIN_DISK_GB=20
MIN_GPU_VRAM_GB=10       # hybrid-auto-engine 最低
MIN_CUDA_MAJOR=12
MIN_DOCKER_VERSION="20.10.0"
GPU_ARCH_MIN_COMPUTE=70   # Volta SM 7.0

# ---- 计数器 ----
TOTAL_CHECKS=0
PASS_CHECKS=0
WARN_CHECKS=0
FAIL_CHECKS=0

# ============================================================
# 工具函数
# ============================================================

check_pass() {
    echo -e "  ${PASS} $1"
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    PASS_CHECKS=$((PASS_CHECKS + 1))
}

check_warn() {
    echo -e "  ${WARN} $1"
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    WARN_CHECKS=$((WARN_CHECKS + 1))
}

check_fail() {
    echo -e "  ${FAIL} $1"
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    FAIL_CHECKS=$((FAIL_CHECKS + 1))
}

print_header() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}============================================================${NC}"
}

print_info() {
    echo -e "  ${INFO} $1"
}

# ============================================================
# 1. 服务器基本信息
# ============================================================
print_header "1. 服务器基本信息"

# 硬件型号
if command -v dmidecode &> /dev/null; then
    VENDOR=$(dmidecode -s system-manufacturer 2>/dev/null || echo "未知")
    PRODUCT=$(dmidecode -s system-product-name 2>/dev/null || echo "未知")
    print_info "制造商: $VENDOR"
    print_info "产品型号: $PRODUCT"
else
    print_info "制造商: 无法获取（dmidecode 不可用）"
fi

# 虚拟化检测
if command -v systemd-detect-virt &> /dev/null; then
    VIRT_TYPE=$(systemd-detect-virt 2>/dev/null || echo "未知")
    print_info "虚拟化类型: $VIRT_TYPE"
    if [ "$VIRT_TYPE" != "none" ]; then
        check_warn "运行在虚拟化环境中（$VIRT_TYPE），GPU 直通需确认"
    else
        check_pass "物理机环境"
    fi
else
    print_info "虚拟化类型: 未知"
fi

# 主板/BIOS 信息
if [ -f /sys/class/dmi/id/bios_version ]; then
    print_info "BIOS 版本: $(cat /sys/class/dmi/id/bios_version 2>/dev/null || echo 未知)"
fi

# ============================================================
# 2. CPU 检查
# ============================================================
print_header "2. CPU 检查"

CPU_CORES=$(nproc)
CPU_MODEL=$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs || echo "未知")
CPU_ARCH=$(uname -m)

print_info "架构: $CPU_ARCH"
print_info "型号: $CPU_MODEL"
print_info "逻辑核心数: $CPU_CORES"

if [ "$CPU_ARCH" != "x86_64" ] && [ "$CPU_ARCH" != "aarch64" ]; then
    check_fail "CPU 架构 $CPU_ARCH 不在官方支持范围内（仅 x86_64 / aarch64）"
else
    check_pass "CPU 架构支持: $CPU_ARCH"
fi

if [ "$CPU_CORES" -ge "$MIN_CPU_CORES" ]; then
    check_pass "CPU 核心数: $CPU_CORES（≥ $MIN_CPU_CORES）"
else
    check_warn "CPU 核心数: $CPU_CORES（建议 ≥ $MIN_CPU_CORES）"
fi

# 检查 AVX2 指令集（torch 需要）
if grep -q avx2 /proc/cpuinfo; then
    check_pass "AVX2 指令集支持"
else
    check_warn "不支持 AVX2 指令集（可能影响 torch 性能）"
fi

# ============================================================
# 3. GPU 检查
# ============================================================
print_header "3. GPU 检查"

if ! command -v nvidia-smi &> /dev/null; then
    check_fail "nvidia-smi 不可用 — 未检测到 NVIDIA GPU 或驱动未安装"
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────┐"
    echo "  │  如使用 pipeline 后端（纯 CPU），GPU 不是必须的。           │"
    echo "  │  如使用 hybrid-auto-engine / vlm-auto-engine，            │"
    echo "  │  必须 NVIDIA GPU（Volta 架构及以上）。                     │"
    echo "  └─────────────────────────────────────────────────────────┘"
else
    # GPU 数量和型号
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)
    print_info "GPU 数量: $GPU_COUNT"

    # 逐 GPU 检查
    GPU_INDEX=0
    while IFS=, read -r gpu_index gpu_name gpu_memory gpu_compute; do
        # 去除首尾空格
        gpu_index=$(echo "$gpu_index" | xargs)
        gpu_name=$(echo "$gpu_name" | xargs)
        gpu_memory=$(echo "$gpu_memory" | xargs)
        gpu_compute=$(echo "$gpu_compute" | xargs)

        echo ""
        echo -e "  ${BLUE}--- GPU $gpu_index: $gpu_name ---${NC}"

        # 显存（MiB → GB 转换）
        GPU_MEM_MIB=$(nvidia-smi --id="$gpu_index" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | xargs)
        GPU_MEM_GB=$((GPU_MEM_MIB / 1024))
        print_info "显存总量: ${GPU_MEM_GB}GB (${GPU_MEM_MIB}MiB)"

        # 已用显存
        GPU_USED_MIB=$(nvidia-smi --id="$gpu_index" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | xargs)
        GPU_FREE_GB=$(((GPU_MEM_MIB - GPU_USED_MIB) / 1024))
        print_info "已用显存: $((GPU_USED_MIB / 1024))GB / ${GPU_MEM_GB}GB（空闲: ${GPU_FREE_GB}GB）"

        if [ "$GPU_MEM_GB" -ge "$MIN_GPU_VRAM_GB" ]; then
            check_pass "显存满足要求: ${GPU_MEM_GB}GB（≥ ${MIN_GPU_VRAM_GB}GB）"
        else
            check_fail "显存不足: ${GPU_MEM_GB}GB（需要 ≥ ${MIN_GPU_VRAM_GB}GB）"
        fi

        # 当前显存使用率
        GPU_USAGE_PCT=$((GPU_USED_MIB * 100 / GPU_MEM_MIB))
        if [ "$GPU_USAGE_PCT" -gt 90 ]; then
            check_warn "当前显存使用率 ${GPU_USAGE_PCT}%（GPU $gpu_index 已接近满载）"
        else
            print_info "当前显存使用率: ${GPU_USAGE_PCT}%"
        fi

        # CUDA 计算能力
        GPU_COMPUTE=$(nvidia-smi --id="$gpu_index" --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | xargs)
        SM_MAJOR=$(echo "$GPU_COMPUTE" | cut -d. -f1)
        SM_MINOR=$(echo "$GPU_COMPUTE" | cut -d. -f2)
        SM_VERSION=$((SM_MAJOR * 10 + SM_MINOR))
        print_info "CUDA 计算能力: $GPU_COMPUTE (SM ${SM_MAJOR}.${SM_MINOR})"

        if [ "$SM_VERSION" -ge "$GPU_ARCH_MIN_COMPUTE" ]; then
            check_pass "GPU 架构满足要求: SM ${SM_MAJOR}.${SM_MINOR}（≥ SM 7.0, Volta）"
        else
            check_fail "GPU 架构不满足: SM ${SM_MAJOR}.${SM_MINOR}（需要 ≥ SM 7.0, Volta）"
        fi

        GPU_INDEX=$((GPU_INDEX + 1))
    done < <(nvidia-smi --query-gpu=index,name,memory.total,compute_cap --format=csv,noheader 2>/dev/null)

    # GPU 总览
    echo ""
    print_info "GPU 总览:"
    nvidia-smi --query-gpu=index,name,memory.total,memory.free,utilization.gpu --format=csv 2>/dev/null | while read -r line; do
        echo "  $line"
    done
fi

# ============================================================
# 4. CUDA 与驱动
# ============================================================
print_header "4. CUDA 与驱动版本"

if command -v nvidia-smi &> /dev/null; then
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | xargs)
    CUDA_VER=$(nvidia-smi | grep "CUDA Version" | awk '{print $NF}')
    print_info "NVIDIA 驱动版本: $DRIVER_VER"
    print_info "CUDA 最高支持版本: $CUDA_VER"

    CUDA_MAJOR=$(echo "$CUDA_VER" | cut -d. -f1)
    if [ "$CUDA_MAJOR" -ge "$MIN_CUDA_MAJOR" ]; then
        check_pass "CUDA 版本满足要求: $CUDA_VER（≥ $MIN_CUDA_MAJOR.x）"
    else
        check_warn "CUDA 版本较低: $CUDA_VER（建议 ≥ $MIN_CUDA_MAJOR.x）"
    fi
else
    check_fail "未检测到 NVIDIA 驱动和 CUDA"
fi

if command -v nvcc &> /dev/null; then
    NVCC_VER=$(nvcc --version | grep "release" | awk '{print $NF}' | tr -d ',')
    print_info "nvcc（CUDA Toolkit）版本: $NVCC_VER"
else
    print_info "nvcc 未安装（Docker 部署不需要，仅在宿主机编译时需要）"
fi

# ============================================================
# 5. 内存检查
# ============================================================
print_header "5. 内存检查"

MEM_TOTAL_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
MEM_TOTAL_GB=$((MEM_TOTAL_KB / 1024 / 1024))
MEM_AVAIL_KB=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
MEM_AVAIL_GB=$((MEM_AVAIL_KB / 1024 / 1024))

print_info "内存总量: ${MEM_TOTAL_GB}GB"
print_info "可用内存: ${MEM_AVAIL_GB}GB"

if [ "$MEM_TOTAL_GB" -ge "$RECOMMENDED_RAM_GB" ]; then
    check_pass "内存满足推荐要求: ${MEM_TOTAL_GB}GB（≥ ${RECOMMENDED_RAM_GB}GB）"
elif [ "$MEM_TOTAL_GB" -ge "$MIN_RAM_GB" ]; then
    check_warn "内存满足最低要求但低于推荐: ${MEM_TOTAL_GB}GB（最低 ${MIN_RAM_GB}GB，推荐 ${RECOMMENDED_RAM_GB}GB）"
else
    check_fail "内存不足: ${MEM_TOTAL_GB}GB（需要 ≥ ${MIN_RAM_GB}GB）"
fi

# Swap
SWAP_TOTAL_KB=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
SWAP_TOTAL_GB=$((SWAP_TOTAL_KB / 1024 / 1024))
if [ "$SWAP_TOTAL_GB" -gt 0 ]; then
    print_info "Swap 总量: ${SWAP_TOTAL_GB}GB"
fi

# ============================================================
# 6. 磁盘检查
# ============================================================
print_header "6. 磁盘检查"

echo ""
echo -e "  ${BLUE}分区概览:${NC}"
df -h --type=ext4 --type=xfs --type=btrfs 2>/dev/null | head -20 | while read -r line; do
    echo "  $line"
done

echo ""
# 检查工作目录所在分区
CURRENT_DIR=$(pwd)
DISK_AVAIL_KB=$(df -k "$CURRENT_DIR" | tail -1 | awk '{print $4}')
DISK_AVAIL_GB=$((DISK_AVAIL_KB / 1024 / 1024))
DISK_TOTAL_KB=$(df -k "$CURRENT_DIR" | tail -1 | awk '{print $2}')
DISK_TOTAL_GB=$((DISK_TOTAL_KB / 1024 / 1024))
DISK_MOUNT=$(df -h "$CURRENT_DIR" | tail -1 | awk '{print $6}')
DISK_FS=$(df -h "$CURRENT_DIR" | tail -1 | awk '{print $1}')

print_info "当前工作目录: $CURRENT_DIR"
print_info "所在分区: $DISK_FS（挂载点: $DISK_MOUNT）"
print_info "分区总容量: ${DISK_TOTAL_GB}GB"
print_info "分区可用: ${DISK_AVAIL_GB}GB"

# MinerU 需要：镜像 ~30GB + 模型 ~5GB + 临时输出
MIN_DISK_FULL_GB=50
if [ "$DISK_AVAIL_GB" -ge "$MIN_DISK_FULL_GB" ]; then
    check_pass "磁盘空间满足完整部署要求: 可用 ${DISK_AVAIL_GB}GB（≥ ${MIN_DISK_FULL_GB}GB）"
elif [ "$DISK_AVAIL_GB" -ge "$MIN_DISK_GB" ]; then
    check_warn "磁盘空间仅满足最低要求: 可用 ${DISK_AVAIL_GB}GB（最低 ${MIN_DISK_GB}GB，完整部署建议 ≥ ${MIN_DISK_FULL_GB}GB）"
else
    check_fail "磁盘空间不足: 可用 ${DISK_AVAIL_GB}GB（需要 ≥ ${MIN_DISK_GB}GB）"
fi

# ============================================================
# 7. 操作系统检查
# ============================================================
print_header "7. 操作系统检查"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    print_info "发行版: $NAME"
    print_info "版本: $VERSION"
    print_info "版本 ID: ${VERSION_ID:-未知}"

    # Linux 需 2019 年及以后
    VERSION_YEAR=$(echo "${VERSION_ID:-0}" | cut -d. -f1)
    if [ "${VERSION_YEAR}" -eq 0 ] 2>/dev/null; then
        # 非数字版本号（如 Ubuntu 的代号），检查 VERSION_ID 中的年份
        VERSION_YEAR=$(echo "$VERSION" | grep -oP '20\d{2}' | head -1 || echo "0")
    fi
    if [ "$VERSION_YEAR" -ge 2019 ] 2>/dev/null; then
        check_pass "操作系统版本满足要求（≥ 2019）"
    elif [ "$VERSION_YEAR" -eq 0 ] 2>/dev/null; then
        print_info "无法自动判断版本年份，跳过（非关键检查）"
    else
        check_warn "操作系统版本可能较旧（$VERSION），MinerU 仅支持 2019 年及以后发行版"
    fi
else
    print_info "无法读取 /etc/os-release"
fi

KERNEL_VER=$(uname -r)
print_info "内核版本: $KERNEL_VER"

# ============================================================
# 8. Docker 环境检查
# ============================================================
print_header "8. Docker 环境检查"

if ! command -v docker &> /dev/null; then
    check_fail "Docker 未安装 — MinerU 推荐使用 Docker 部署"
else
    DOCKER_VER=$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')
    print_info "Docker 版本: $DOCKER_VER"
    check_pass "Docker 已安装"

    # Docker daemon 状态
    if docker info &> /dev/null; then
        check_pass "Docker daemon 运行中"
        DOCKER_ROOT=$(docker info 2>/dev/null | grep "Docker Root Dir" | awk '{print $NF}')
        print_info "Docker 数据目录: $DOCKER_ROOT"
    else
        check_fail "Docker daemon 未运行"
    fi
fi

# Docker Compose
if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    COMPOSE_VER=$(docker compose version 2>/dev/null | awk '{print $NF}' | head -1)
    print_info "Docker Compose 版本: $COMPOSE_VER"
    check_pass "Docker Compose（插件）已安装"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_VER=$(docker-compose --version 2>/dev/null | awk '{print $NF}' | head -1)
    print_info "Docker Compose（独立）版本: $COMPOSE_VER"
    check_warn "使用旧版 docker-compose，建议升级到 docker compose 插件"
else
    check_warn "Docker Compose 未安装（非必须，但推荐）"
fi

# ============================================================
# 9. NVIDIA 容器运行时
# ============================================================
print_header "9. NVIDIA 容器运行时"

# nvidia-container-toolkit
if command -v nvidia-container-toolkit &> /dev/null; then
    NCT_VER=$(nvidia-container-toolkit --version 2>/dev/null || echo "未知")
    print_info "nvidia-container-toolkit: $NCT_VER"
    check_pass "nvidia-container-toolkit 已安装"
else
    check_warn "nvidia-container-toolkit 未安装（Docker GPU 访问需要）"
fi

# nvidia-container-runtime
if command -v nvidia-container-runtime &> /dev/null; then
    check_pass "nvidia-container-runtime 可用"
else
    # 检查 Docker 是否配置了 nvidia runtime
    if docker info 2>/dev/null | grep -qi nvidia; then
        check_pass "Docker NVIDIA runtime 已配置"
    else
        check_warn "Docker NVIDIA runtime 可能未配置"
    fi
fi

# 测试 GPU 是否可在 Docker 中访问
echo ""
print_info "测试 Docker GPU 访问..."
if docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi &> /dev/null; then
    check_pass "Docker 可正常访问 GPU"
else
    check_warn "Docker GPU 访问测试失败（可能需要 sudo 或配置 nvidia runtime）"
fi

# ============================================================
# 10. 已运行容器检查
# ============================================================
print_header "10. 已运行容器（端口冲突检查）"

RUNNING_CONTAINERS=$(docker ps --format 'table {{.Names}}\t{{.Ports}}' 2>/dev/null | tail -n +2)
if [ -n "$RUNNING_CONTAINERS" ]; then
    echo "$RUNNING_CONTAINERS" | while read -r line; do
        print_info "$line"
    done

    # 检查常用端口
    echo ""
    for PORT in 8000 8010 8011 7860 30000; do
        if echo "$RUNNING_CONTAINERS" | grep -q ":$PORT->"; then
            check_warn "端口 $PORT 已被占用"
        fi
    done
else
    print_info "无运行中的容器"
fi

# ============================================================
# 11. 网络连通性（离线部署可跳过）
# ============================================================
print_header "11. 网络连通性检查（仅在线构建时需要）"

PING_TARGETS=(
    "pypi.org|PyPI（Python 包）"
    "github.com|GitHub（代码仓库）"
    "huggingface.co|HuggingFace（模型下载）"
    "modelscope.cn|ModelScope（模型下载）"
)

for target in "${PING_TARGETS[@]}"; do
    HOST="${target%%|*}"
    NAME="${target##*|}"
    if ping -c1 -W2 "$HOST" &> /dev/null; then
        check_pass "$NAME — 可达"
    else
        check_warn "$NAME — 不可达（如为离线部署可忽略）"
    fi
done

# ============================================================
# 最终汇总
# ============================================================
print_header "检查结果汇总"

echo ""
echo "  总检查项: $TOTAL_CHECKS"
echo -e "  ${GREEN}通过: $PASS_CHECKS${NC}"
echo -e "  ${YELLOW}警告: $WARN_CHECKS${NC}"
echo -e "  ${RED}失败: $FAIL_CHECKS${NC}"
echo ""

if [ "$FAIL_CHECKS" -eq 0 ] && [ "$WARN_CHECKS" -eq 0 ]; then
    echo -e "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${GREEN}  🎉 服务器完全满足 MinerU 部署要求！${NC}"
    echo -e "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
elif [ "$FAIL_CHECKS" -eq 0 ]; then
    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${YELLOW}  ✅ 服务器满足最低要求，但有 $WARN_CHECKS 项警告${NC}"
    echo -e "  ${YELLOW}  建议处理警告后再部署${NC}"
    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
else
    echo -e "  ${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${RED}  ❌ 服务器不满足要求，有 $FAIL_CHECKS 项失败${NC}"
    echo -e "  ${RED}  请先解决失败项后再部署${NC}"
    echo -e "  ${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
fi

echo ""
echo "  建议的部署方案："

# 根据 GPU 情况推荐
if [ "${GPU_COUNT:-0}" -ge 2 ]; then
    echo "    - 多 GPU 环境：可部署多个 mineru-api 实例（负载均衡）"
    echo "    - 或使用 MinerU Router 多服务负载均衡"
elif [ "${GPU_COUNT:-0}" -eq 1 ]; then
    echo "    - 单 GPU 环境：部署单个 mineru-api 实例"
else
    echo "    - 无 GPU 环境：使用 pipeline 后端（纯 CPU）"
fi

# 根据内存推荐
if [ "${MEM_TOTAL_GB:-0}" -ge 64 ]; then
    echo "    - 内存充裕：可设置 MINERU_PROCESSING_WINDOW_SIZE=128 提升并发"
fi

echo ""
echo "  脚本执行完毕。"
echo "  详细部署教程请参考: deploy/mineru-3.4.4-offline-deployment-guide.md"
