# MinerU NVIDIA（CUDA）方式 A —— 构建教程（构建机专用）

> **面向对象**：构建 / 开发人员
> **本文件只在构建机用，不随包分发到服务器**。部署人员看 `README-internal-deploy.md`。
> **产出**：`offline-package/` 下完整的离线部署物料包（自包含镜像 tar.gz + 配置文件）

---

## 前置

- 构建机有 Docker ≥ 24.0（需支持 `--build-context`，Docker 28+ 最佳）
- 本地已有 `/zhangbo/mineru_models/`（含全部模型，包含 OriCls）
- 磁盘可用 ≥ 50GB（镜像含模型约 34GB，需额外空间给构建缓存）
- **代码 + 模型均为本地**（镜像用仓库 `mineru/` 源码，模型通过 `--build-context` 传入）
- **首次构建可能需要联网**（`pip install -e ".[core]"` 若缺依赖需从 PyPI 拉取；如本机已有 vllm 基础镜像则通常已满足）

## 构建

```bash
cd <仓库根目录>   # 例如 /zhangbo/MinerU
bash offline-package/build-nvidia.sh
```

> 脚本会依次：
> 1. 检查前置条件（Docker 版本、模型目录、磁盘空间）
> 2. 使用 `offline-package/nvidia-fork.Dockerfile` 构建镜像，通过 `--build-context` 把本地模型烤进镜像
> 3. 导出镜像为单个 `mineru-3.4.4-upstream.tar.gz`（~12GB 压缩后，含全部模型）
> 4. 复制 compose / 部署指南 / 环境检查脚本
> 5. 生成 MANIFEST.txt

> 镜像 TAG 沿用 `mineru:3.4.4-upstream`（与 PPU 版 `mineru:ppu-fork-3.4.4` 对应）。

验证镜像已构建：

```bash
docker images mineru:3.4.4-upstream
# 约 34GB（含模型）
```

验证镜像内模型完整：

```bash
docker run --rm --entrypoint "" mineru:3.4.4-upstream \
  ls /data/mineru_models/hub/models/OpenDataLab/
# 应该看到 PDF-Extract-Kit-1___0 和 MinerU2___5-Pro-2605-1___2B
```

## 更新代码（改代码后重打）

```bash
bash offline-package/build-nvidia.sh
```

> 脚本会**重建镜像**（更新 fork 代码）并**重新烤入模型**。模型文件不变时，Docker 缓存层会加速构建。

## 和 build-ppu-fork.sh 的对应关系

| | PPU（平头哥） | NVIDIA（CUDA） |
|---|---|---|
| 脚本 | `offline-package-ppu/build-ppu-fork.sh` | **`offline-package/build-nvidia.sh`** |
| 构建教程 | `README-ppu-build.md` | **`README-nvidia-build.md`**（本文件）|
| 镜像 TAG | `mineru:ppu-fork-3.4.4` | `mineru:3.4.4-upstream` |
| 基础镜像 | `mineru:ppu-vllm-latest`（PPU 驱动 + 模型已在其中）| `vllm/vllm-openai:v0.11.2`（CUDA 基础，不含模型）|
| 模型 | 烤进基础镜像（43GB，从零构建） | 通过 `--build-context` 烤入 nvidia-fork.Dockerfile（~34GB） |
| 产出 | 单个 tar.gz（自包含，含模型） | 单个 tar.gz（自包含，含模型） |
| 部署 | docker load + compose up（无需挂载） | docker load + compose up（无需挂载） |

> 两个脚本的设计理念一致：**方式 A = 自包含单包，开箱即用，无需挂载**。
> 区别只在于 PPU 使用两阶段构建（基础镜像已有模型，薄层覆盖代码），
> NVIDIA 使用 `--build-context` 在同一次构建中烤入模型。

## 交付清单（发给部署人员）

把 `offline-package/` 整个目录发给部署人员（放到服务器 `/data/` 下）：

```
offline-package/
├── mineru-3.4.4-upstream.tar.gz  ← Docker 镜像（含模型，自包含）
├── compose-prod.yaml             ← 启动配置（无需模型挂载）
├── check_server_env.sh           ← 环境检查脚本
├── README-internal-deploy.md     ← 部署教程（部署人员读这个）
├── MANIFEST.txt                  ← 物料清单
└── README-nvidia-build.md        ← 本文件（不随包分发，可删除）