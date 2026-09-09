#!/bin/bash
# ============================================================
# MinerU PPU（平头哥 T-Head / alixpu）服务器环境检查脚本
#
# 用途：方式 A / 方式 B 部署前，在【目标服务器】上先跑一遍，
#       确认 PPU 硬件、基础镜像、数据路径、端口是否满足要求。
# 用法：bash check_server_env.sh
#
# 检查项：
#   1. 基本信息（主机名、内核）
#   2. Docker / Docker Compose
#   3. PPU 设备节点（/dev/alixpu、/dev/alixpu_ctl）
#   4. ppu-smi（卡列表，供填 CUDA_VISIBLE_DEVICES）
#   5. 基础镜像（mineru:ppu-vllm-latest 是否存在 + 内部 mineru 版本）
#   6. 数据/输出路径（/mnt、/datapool）
#   7. 内存 / /dev/shm（shm_size 500g 前提）
#   8. 端口占用（8000 / 8009）
#   9. 已运行容器
# ============================================================
set -euo pipefail

# ---- 颜色定义 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS="${GREEN}✅ PASS${NC}"
WARN="${YELLOW}⚠️  WARN${NC}"
FAIL="${RED}❌ FAIL${NC}"
INFO="${BLUE}ℹ️  INFO${NC}"

# ---- 基础镜像名（与 README / compose 一致）----
BASE_IMAGE="${BASE_IMAGE:-mineru:ppu-vllm-latest}"
EXPECT_VERSION="3.4.4"

# ---- 计数器 ----
TOTAL_CHECKS=0
PASS_CHECKS=0
WARN_CHECKS=0
FAIL_CHECKS=0

check_pass() { echo -e "  ${PASS} $1"; TOTAL_CHECKS=$((TOTAL_CHECKS+1)); PASS_CHECKS=$((PASS_CHECKS+1)); }
check_warn() { echo -e "  ${WARN} $1"; TOTAL_CHECKS=$((TOTAL_CHECKS+1)); WARN_CHECKS=$((WARN_CHECKS+1)); }
check_fail() { echo -e "  ${FAIL} $1"; TOTAL_CHECKS=$((TOTAL_CHECKS+1)); FAIL_CHECKS=$((FAIL_CHECKS+1)); }

print_header() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}============================================================${NC}"
}
print_info() { echo -e "  ${INFO} $1"; }

# ============================================================
# 1. 基本信息
# ============================================================
print_header "1. 基本信息"
print_info "主机名: $(hostname 2>/dev/null || echo 未知)"
print_info "内核: $(uname -r)"
print_info "架构: $(uname -m)"

# ============================================================
# 2. Docker / Docker Compose
# ============================================================
print_header "2. Docker / Docker Compose"

if ! command -v docker &> /dev/null; then
    check_fail "Docker 未安装"
else
    DOCKER_VER=$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')
    print_info "Docker 版本: $DOCKER_VER"
    check_pass "Docker 已安装"

    if docker info &> /dev/null; then
        check_pass "Docker daemon 运行中"
    else
        check_fail "Docker daemon 未运行"
    fi
fi

if command -v docker &> /dev/null && docker compose version &> /dev/null; then
    COMPOSE_VER=$(docker compose version 2>/dev/null | awk '{print $NF}' | head -1)
    print_info "Docker Compose（插件）版本: $COMPOSE_VER"
    check_pass "Docker Compose 已安装"
elif command -v docker-compose &> /dev/null; then
    print_info "使用旧版 docker-compose（建议升级到 docker compose 插件）"
    check_warn "使用旧版 docker-compose"
else
    check_warn "Docker Compose 未安装（本目录用 docker compose 启动，需要）"
fi

# ============================================================
# 3. PPU 设备节点
# ============================================================
print_header "3. PPU 设备节点"

if [ -e /dev/alixpu ]; then
    print_info "/dev/alixpu 存在: $(ls -l /dev/alixpu | awk '{print $1, $3, $4}')"
    check_pass "/dev/alixpu 设备节点存在"
else
    check_fail "/dev/alixpu 不存在（PPU 卡未识别/驱动未装）"
fi

if [ -e /dev/alixpu_ctl ]; then
    print_info "/dev/alixpu_ctl 存在"
    check_pass "/dev/alixpu_ctl 设备节点存在"
else
    check_fail "/dev/alixpu_ctl 不存在"
fi

# ============================================================
# 4. ppu-smi
# ============================================================
print_header "4. ppu-smi（仅查看卡状态；实际指定卡用 CUDA_VISIBLE_DEVICES）"

if command -v ppu-smi &> /dev/null; then
    check_pass "ppu-smi 可用"
    print_info "ppu-smi 输出："
    ppu-smi 2>&1 | head -30 | while read -r line; do echo "  | $line"; done || true
else
    check_warn "ppu-smi 不可用（无法查看卡号/空闲卡，请与阿里确认卡序号）"
fi

# ============================================================
# 5. 基础镜像 + 内部 mineru 版本
# ============================================================
print_header "5. 基础镜像 ${BASE_IMAGE}"

if docker image inspect "${BASE_IMAGE}" &> /dev/null; then
    check_pass "基础镜像 ${BASE_IMAGE} 已存在"
    # 版本是方式 A/B 选型的核心依据
    VER=$(docker run --rm "${BASE_IMAGE}" python3 -c "import mineru; print(mineru.__version__)" 2>/dev/null || echo "unknown")
    print_info "镜像内 mineru 版本: ${VER}"
    if [ "${VER}" = "${EXPECT_VERSION}" ]; then
        check_pass "镜像内 mineru 版本 = ${EXPECT_VERSION}（可走方式 B 源码挂载）"
    else
        check_warn "镜像内 mineru 版本 = ${VER}（≠ ${EXPECT_VERSION}，方式 B 挂载会缺依赖 → 建议改用方式 A，见 README.md）"
    fi
else
    check_warn "基础镜像 ${BASE_IMAGE} 不存在（方式 B 需要；方式 A 不需要，在构建机重建并打 fork 镜像）"
fi

# ============================================================
# 5b. 镜像内 OriCls 模型（部署前确认，缺了解析会报错）
# ============================================================
ORCLS_PATH="/root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx"

for IMG in "${BASE_IMAGE}" "mineru:ppu-fork-3.4.4"; do
    if docker image inspect "${IMG}" &> /dev/null; then
        if docker run --rm "${IMG}" test -e "${ORCLS_PATH}" 2>/dev/null; then
            print_info "${IMG} 含 OriCls 模型"
            check_pass "${IMG} 含 OriCls 模型（方向分类模型齐全）"
        else
            print_info "${IMG} 缺 OriCls 模型"
            check_warn "${IMG} 缺 OriCls 模型（2026-09-08 前构建的旧镜像；补齐见 README-ppu-deploy.md「镜像缺 OriCls 怎么办」）"
        fi
    fi
done

# ============================================================
# 6. 数据 / 输出路径
# ============================================================
print_header "6. 数据 / 输出路径"

for P in /mnt /datapool; do
    if [ -d "$P" ]; then
        AVAIL_GB=$(df -k "$P" 2>/dev/null | tail -1 | awk '{print int($4/1024/1024)}')
        print_info "$P 存在，可用约 ${AVAIL_GB}GB"
        check_pass "$P 存在"
    else
        check_fail "$P 不存在（compose 挂载 /mnt、/datapool，请按服务器实际路径改 compose）"
    fi
done

# ============================================================
# 7. 内存 / /dev/shm
# ============================================================
print_header "7. 内存 / /dev/shm（shm_size 500g 前提）"

MEM_TOTAL_GB=$(awk '/MemTotal/{print int($2/1024/1024)}' /proc/meminfo)
MEM_AVAIL_GB=$(awk '/MemAvailable/{print int($2/1024/1024)}' /proc/meminfo)
print_info "内存总量: ${MEM_TOTAL_GB}GB / 可用: ${MEM_AVAIL_GB}GB"

SHM_GB=$(df -k /dev/shm 2>/dev/null | tail -1 | awk '{print int($2/1024/1024)}')
print_info "/dev/shm 总量: ${SHM_GB:-未知}GB（compose 要求 shm_size 500g，若不足需调小）"

if [ "${MEM_TOTAL_GB:-0}" -ge 64 ]; then
    check_pass "内存充裕（≥64GB）"
elif [ "${MEM_TOTAL_GB:-0}" -ge 32 ]; then
    check_warn "内存 ${MEM_TOTAL_GB}GB（建议 ≥64GB，PPU vLLM 吃内存）"
else
    check_fail "内存不足 ${MEM_TOTAL_GB}GB（建议 ≥64GB）"
fi

# ============================================================
# 8. 端口占用
# ============================================================
print_header "8. 端口占用（mineru-api 监听 8000，旧文档用 8009）"

if command -v ss &> /dev/null; then
    for PORT in 8000 8009; do
        if ss -tln 2>/dev/null | grep -q ":${PORT} "; then
            check_warn "端口 ${PORT} 已被占用（启动前先确认监听端口/IP）"
        else
            print_info "端口 ${PORT} 空闲"
            check_pass "端口 ${PORT} 空闲"
        fi
    done
else
    check_warn "ss 不可用，请手动确认 8000/8009 端口占用"
fi

# ============================================================
# 9. 已运行容器
# ============================================================
print_header "9. 已运行容器"

if command -v docker &> /dev/null && docker ps &> /dev/null; then
    RUNNING=$(docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | tail -n +2)
    if [ -n "$RUNNING" ]; then
        echo "$RUNNING" | while read -r line; do print_info "$line"; done
    else
        print_info "无运行中的容器"
    fi
fi

# ============================================================
# 汇总
# ============================================================
print_header "检查结果汇总"

echo ""
echo "  总检查项: ${TOTAL_CHECKS}"
echo -e "  ${GREEN}通过: ${PASS_CHECKS}${NC}"
echo -e "  ${YELLOW}警告: ${WARN_CHECKS}${NC}"
echo -e "  ${RED}失败: ${FAIL_CHECKS}${NC}"
echo ""

if [ "${FAIL_CHECKS}" -eq 0 ]; then
    echo -e "  ${GREEN}✅ 环境满足要求，可按 README 继续部署${NC}"
else
    echo -e "  ${RED}❌ 有 ${FAIL_CHECKS} 项失败，请先解决后重新跑本脚本${NC}"
fi

echo ""
echo "  下一步选型："
echo "    - 基础镜像 mineru 版本 = ${EXPECT_VERSION} → 走方式 B（README-ppu-mount-deploy.md）"
echo "    - 版本旧 / 不确定 / 想固化 → 走方式 A（README-ppu-deploy.md）"
echo ""
echo "  脚本执行完毕。"
