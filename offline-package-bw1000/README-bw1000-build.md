# MinerU 海光 BW1000（深算三号 DCU）方式 A —— 构建教程（构建机专用）

> **面向对象**：构建 / 开发人员（在**联网构建机**上操作）
> **本文件只在构建机用，不随包分发到服务器**。部署人员看 `README-bw1000-deploy.md`。
> **产出**：`mineru-bw1000-fork-3.4.4.tar.gz`（fork 代码烤进 BW1000 DCU 镜像的离线包）

---

## 前置

- 构建机有 Docker（≥24.0，支持 `docker compose`），能联网。
- 基座镜像来自**源申 Harbor**（`harbor.sourcefind.cn:5443/dcu/admin/base/`），需可拉取。
- 构建机磁盘可用 ≥ 80GB（镜像含模型约 40-50GB，需额外空间给构建缓存）。
- BW1000（深算三号）需 DTK ≥ 25.04 + rock-6.3.8+ 驱动（基座镜像应已包含 DTK；驱动在宿主机安装，不在镜像内）。

## 步骤 1：重建基础镜像（一次性，耗时较长）

```bash
cd <仓库根目录>   # 例如 /zhangbo/MinerU
docker build --network=host -t mineru:bw1000-vllm-latest -f docker/china/bw1000.Dockerfile .
```

> 该步联网 `pip install 'mineru[core]>=3.0.0'`（解析到 3.4.4）+ `mineru-models-download` 下模型，耗时十几分钟~几十分钟。基础镜像重建过一次后，后续更新只需重跑步骤 2。

> ⚠️ **基座镜像选择**：`docker/china/bw1000.Dockerfile` 的 `FROM` 指向了 DTK 25.04.2 的 vLLM 基座镜像；如果 BW1000 需要更新的 DTK 版本（如 26.04），请修改 `FROM` 行。基座镜像获取方式：
> - 光合开发者社区（[developer.hpccube.com](https://developer.hpccube.com)）
> - 源申 Harbor（`harbor.sourcefind.cn`）

> ⚠️ **模型自动下载**：构建过程中会通过 `mineru-models-download -s modelscope -m all` 下载 Pipeline + VLM 模型（~34GB），**之后另有 `snapshot_download` 补下载 OriCls 方向分类模型**（上游下载列表缺失该模型）。确保构建机网络可达 modelscope.cn。

## 步骤 2：薄层 COPY fork 源码 + 导出 tar.gz

```bash
bash offline-package-bw1000/build-bw1000.sh
# 产物：offline-package-bw1000/mineru-bw1000-fork-3.4.4.tar.gz
```

> `build-bw1000.sh` 会先检查基础镜像 `mineru:bw1000-vllm-latest` 是否存在，缺了就报错提醒先跑步骤 1。

验证 fork 代码已烤入：

```bash
docker run --rm mineru:bw1000-fork-3.4.4 \
  python3 -c "import mineru.utils.custom.table_utils; print('table_utils ok')"
```

## 更新代码（改代码后重打）

- 只改 `mineru/` 源码 → 重跑**步骤 2**（`build-bw1000.sh`）。
- 改了 `pyproject.toml` 依赖 → 重跑**步骤 1 + 2**（重建基础镜像）。

## 与 NVIDIA / PPU 对应关系

| 特性 | PPU（平头哥 T-Head） | NVIDIA（CUDA） | **海光 BW1000（DCU）** |
|------|---------------------|----------------|----------------------|
| 基础镜像 Dockerfile | `docker/china/ppu.Dockerfile` | `nvidia-fork.Dockerfile`（直接一次构建） | **`docker/china/bw1000.Dockerfile`** |
| 薄层覆盖 Dockerfile | `ppu-fork.Dockerfile` | — | **`bw1000-fork.Dockerfile`** |
| 构建脚本 | `build-ppu-fork.sh` | `build-nvidia.sh` | **`build-bw1000.sh`** |
| 镜像 TAG | `mineru:ppu-fork-3.4.4` | `mineru:3.4.4-upstream` | **`mineru:bw1000-fork-3.4.4`** |
| 基座来源 | 阿里云 ACR（上海） | vllm/vllm-openai（Docker Hub） | **源申 Harbor / 光合开发者社区** |
| 设备节点 | `/dev/alixpu` | —（NVIDIA） | `/dev/hycu*`（rock-6.3.x+） |
| 监控工具 | `ppu-smi` | `nvidia-smi` | **`dcu-smi`** |

## 交付清单（发给部署人员）

把下面文件发给部署人员（放到服务器 `/data/` 下）：

```
mineru-bw1000-fork-3.4.4.tar.gz   ← 镜像（docker load 用）
compose-bw1000.yaml               ← 启动配置
check_server_env.sh               ← 环境检查脚本
README-bw1000-deploy.md           ← 部署教程（部署人员读这个）
MANIFEST-bw1000.txt               ← 物料清单
```

> 构建机专用文件（`bw1000-fork.Dockerfile`、`build-bw1000.sh`、`README-bw1000-build.md`）不随包分发。