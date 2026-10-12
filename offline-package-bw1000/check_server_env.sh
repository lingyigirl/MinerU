#!/bin/bash
# ============================================================
# MinerU 海光 BW1000（深算三号 DCU）服务器环境检查脚本
#
# 用途：方式 A 部署前，在【目标服务器】上先跑一遍，
#       确认 DCU 硬件、基础镜像、数据路径、端口是否满足要求。
# 用法：bash check_server_env.sh
#
# 检查项：
#   1. 基本信息（主机名、内核）
#   2. Docker / Docker Compose
#   3. DCU 设备节点（/dev/hycu*、/dev/dri 等）
#   4. dcu-smi（卡列表，供填 CUDA_VISIBLE_DEVICES）
#   5. 基础镜像（mineru:bw1000-fork-3.4.4 是否存在）
#   6. 数据/输出路径
#   7. 内存 / /dev/shm（shm_size 500g 前提）
#   8. 端口占用（8000）
#   9. 已运行容器
# ============================================================
set -euo pipefail

# ---- 颜色定义 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS="${GREEN}✅ PASS${NC}"; WARN="${YELLOW}⚠️  WARN${NC}"; FAIL="${RED}❌ FAIL${NC}"; INFO="${BLUE}ℹ️  INFO${NC}"

TARGET_IMAGE="${TARGET_IMAGE:-mineru:bw1000-fork-3.4.4}"
EXPECT_VERSION="3.4.4"

TOTAL_CHECKS=0; PASS_CHECKS=0; WARN_CHECKS=0; FAIL_CHECKS=0

check_pass() { echo -e "  ${PASS} $1"; ((TOTAL_CHECKS++, PASS_CHECKS++)); }
check_warn() { echo -e "  ${WARN} $1"; ((TOTAL_CHECKS++, WARN_CHECKS++)); }
check_fail() { echo -e "  ${FAIL} $1"; ((TOTAL_CHECKS++, FAIL_CHECKS++)); }
print_header() { echo ""; echo -e "${BLUE}============================================================${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}============================================================${NC}"; }
print_info() { echo -e "  ${INFO} $1"; }

# ============================================================
# 1. 基本信息
# ============================================================
print_header "1. 基本信息"
print_info "主机名: $(hostname 2>/dev/null || echo 未知)"
print_info "内核: $(uname -r)"
print_info "架构: $(uname -m)"
print_info "日期: $(date '+%Y-%m-%d %H:%M:%S')"

# CPU 信息
CPU_CORES=$(nproc)
print_info "CPU 逻辑核心数: $CPU_CORES"
CPU_ARCH=$(uname -m)
if [ "$CPU_ARCH" = "x86_64" ] || [ "$CPU_ARCH" = "aarch64" ]; then
    check_pass "CPU 架构: $CPU_ARCH（海光 C86 x86-64 兼容）"
else
    check_fail "CPU 架构: $CPU_ARCH（不支持）"
fi

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
    check_warn "Docker Compose 未安装（部署需要）"
fi

# ============================================================
# 3. DCU 设备节点
# ============================================================
print_header "3. 海光 DCU 设备节点"
print_info "驱动模块检查（hycu 用于 rock-6.3.x+，hydcu 用于 6.2.x-）："

HYCU_LOADED=false; HYDCU_LOADED=false
if lsmod 2>/dev/null | grep -q "hycu"; then
    HYCU_LOADED=true; check_pass "hycu 内核模块已加载（rock-6.3.x+ 驱动）"
fi
if lsmod 2>/dev/null | grep -q "hydcu"; then
    HYDCU_LOADED=true; print_info "hydcu 内核模块已加载（rock-6.2.x- 驱动）"
fi
if ! $HYCU_LOADED && ! $HYDCU_LOADED; then
    check_warn "未检测到 hycu/hydcu 内核模块（DCU 驱动可能未安装或未加载）"
fi

# 设备节点扫描
DEV_FOUND=false
# 检查 /dev/hycu* 节点（rock-6.3.x+）
for d in /dev/hycu*; do
    if [ -e "$d" ]; then
        print_info "$d 存在: $(ls -l "$d" | awk '{print $1, $3, $4, $NF}' 2>/dev/null)"
        DEV_FOUND=true
    fi
done
# 检查 /dev/hydcu* 节点（rock-6.2.x-）
for d in /dev/hydcu*; do
    if [ -e "$d" ]; then
        print_info "$d 存在"
        DEV_FOUND=true
    fi
done
# 检查 /dev/dri/（部分 DCU 驱动通过 kfd/dri 暴露）
if [ -e /dev/dri ]; then
    print_info "/dev/dri 存在: $(ls /dev/dri/ 2>/dev/null | tr '\n' ' ')"
fi

if $DEV_FOUND || $HYCU_LOADED || $HYDCU_LOADED; then
    check_pass "DCU 设备可访问"
else
    check_fail "未检测到任何 DCU 设备节点或驱动（确认 DCU 卡已安装、驱动已加载）"
fi

# ============================================================
# 4. dcu-smi
# ============================================================
print_header "4. dcu-smi（DCU 卡状态）"
if command -v dcu-smi &> /dev/null; then
    check_pass "dcu-smi 可用"
    print_info "dcu-smi 输出："
    dcu-smi 2>&1 | head -40 | while read -r line; do echo "  | $line"; done || true
else
    # 尝试其他别名
    if command -v hy-smi &> /dev/null; then
        check_pass "hy-smi 可用"
        hy-smi 2>&1 | head -40 | while read -r line; do echo "  | $line"; done || true
    else
        check_warn "dcu-smi / hy-smi 不可用（无法查看卡号，请与管理员确认 DCU 卡序号）"
    fi
fi

# ============================================================
# 5. 部署镜像
# ============================================================
print_header "5. 部署镜像 ${TARGET_IMAGE}"
if docker image inspect "${TARGET_IMAGE}" &> /dev/null; then
    check_pass "部署镜像 ${TARGET_IMAGE} 已存在"
    VER=$(docker run --rm "${TARGET_IMAGE}" python3 -c "import mineru; print(mineru.__version__)" 2>/dev/null || echo "unknown")
    print_info "镜像内 mineru 版本: ${VER}"
    if [ "${VER}" = "${EXPECT_VERSION}" ]; then
        check_pass "mineru 版本 = ${EXPECT_VERSION}"
    else
        check_warn "mineru 版本 = ${VER}（≠ ${EXPECT_VERSION}，可能有兼容性问题）"
    fi
else
    check_warn "部署镜像 ${TARGET_IMAGE} 不存在，需先 docker load"
fi

# 5b. 镜像内 OriCls 模型
ORCLS_PATH="/root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/paddle_orientation_classification/PP-LCNet_x1_0_doc_ori.onnx"
if docker image inspect "${TARGET_IMAGE}" &> /dev/null; then
    if docker run --rm "${TARGET_IMAGE}" test -e "${ORCLS_PATH}" 2>/dev/null; then
        print_info "${TARGET_IMAGE} 含 OriCls 模型"
        check_pass "OriCls 方向分类模型齐全"
    else
        check_warn "${TARGET_IMAGE} 缺 OriCls 模型（请确认基础镜像构建版本）"
    fi
fi

# ============================================================
# 6. 数据 / 输出路径
# ============================================================
print_header "6. 数据 / 输出路径"
CHECK_DIRS=("/data/mineru_output" "/data")
for P in "${CHECK_DIRS[@]}"; do
    if [ -d "$P" ]; then
        AVAIL_GB=$(df -k "$P" 2>/dev/null | tail -1 | awk '{print int($4/1024/1024)}')
        print_info "$P 存在，可用约 ${AVAIL_GB}GB"
    else
        if [ "$P" = "/data/mineru_output" ]; then
            print_info "$P 不存在（将在 docker compose up 前创建）"
        fi
    fi
done

# 工作目录磁盘
CURRENT_DIR=$(pwd)
DISK_AVAIL_GB=$(df -k "$CURRENT_DIR" | tail -1 | awk '{print int($4/1024/1024)}')
DISK_TOTAL_GB=$(df -k "$CURRENT_DIR" | tail -1 | awk '{print int($2/1024/1024)}')
print_info "当前目录所在分区: ${CURRENT_DIR}（总 ${DISK_TOTAL_GB}GB，可用 ${DISK_AVAIL_GB}GB）"
if [ "$DISK_AVAIL_GB" -ge 50 ]; then
    check_pass "磁盘可用 ${DISK_AVAIL_GB}GB（≥ 50GB）"
elif [ "$DISK_AVAIL_GB" -ge 20 ]; then
    check_warn "磁盘可用 ${DISK_AVAIL_GB}GB（建议 ≥ 50GB）"
else
    check_fail "磁盘可用 ${DISK_AVAIL_GB}GB（< 20GB）"
fi

# ============================================================
# 7. 内存 / /dev/shm
# ============================================================
print_header "7. 内存 / /dev/shm"
MEM_TOTAL_GB=$(awk '/MemTotal/{print int($2/1024/1024)}' /proc/meminfo)
MEM_AVAIL_GB=$(awk '/MemAvailable/{print int($2/1024/1024)}' /proc/meminfo)
print_info "内存总量: ${MEM_TOTAL_GB}GB / 可用: ${MEM_AVAIL_GB}GB"

if [ "${MEM_TOTAL_GB:-0}" -ge 64 ]; then
    check_pass "内存充裕（≥64GB）"
elif [ "${MEM_TOTAL_GB:-0}" -ge 32 ]; then
    check_warn "内存 ${MEM_TOTAL_GB}GB（建议 ≥64GB，DCU vLLM 吃内存）"
else
    check_fail "内存不足 ${MEM_TOTAL_GB}GB（建议 ≥64GB）"
fi

SHM_GB=$(df -k /dev/shm 2>/dev/null | tail -1 | awk '{print int($2/1024/1024)}')
if [ -n "$SHM_GB" ]; then
    print_info "/dev/shm 总量: ${SHM_GB}GB（compose 要求 shm_size 500g）"
fi

# ============================================================
# 8. 端口占用
# ============================================================
print_header "8. 端口占用（mineru-api 默认 8000）"
for PORT in 8000 8001; do
    if command -v ss &> /dev/null; then
        if ss -tln 2>/dev/null | grep -q ":${PORT} "; then
            check_warn "端口 ${PORT} 已被占用"
        else
            check_pass "端口 ${PORT} 空闲"
        fi
    elif command -v netstat &> /dev/null; then
        if netstat -tln 2>/dev/null | grep -q ":${PORT} "; then
            check_warn "端口 ${PORT} 已被占用"
        else
            check_pass "端口 ${PORT} 空闲"
        fi
    fi
done

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
    echo -e "  ${GREEN}✅ 环境满足要求，可按 README-bw1000-deploy.md 继续部署${NC}"
else
    echo -e "  ${RED}❌ 有 ${FAIL_CHECKS} 项失败，请先解决后重新跑本脚本${NC}"
fi
echo ""
echo "  脚本执行完毕。"