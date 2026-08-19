# MinerU 离线部署包索引

> `deploy/` 是**构建与文档源**（`build-offline-package.sh`、各 compose / 模板从这里分发）。
> 真正交付给部署人员的物料包在**仓库根目录**的 4 个 `offline-package*` 文件夹。
> 本文件是 4 个包的入口索引。

## 4 个物料包

| 加速卡 | 方式 A（烤代码进镜像） | 方式 B（源码挂载） |
|--------|----------------------|-------------------|
| NVIDIA GPU（CUDA） | [offline-package/](../offline-package/) | [offline-package-mount/](../offline-package-mount/) |
| T-Head PPU（平头哥） | [offline-package-ppu/](../offline-package-ppu/) | [offline-package-ppu-mount/](../offline-package-ppu-mount/) |

## 部署入口（部署人员读「deploy」版）

| 包 | 部署教程 | 物料清单 | 镜像 TAG |
|----|---------|---------|---------|
| NVIDIA 方式 A | [README-internal-deploy.md](README-internal-deploy.md) | `offline-package/MANIFEST.txt` | `mineru:3.4.4-upstream` |
| NVIDIA 方式 B | [README-mount-deploy.md](README-mount-deploy.md) | `offline-package-mount/MANIFEST-mount.txt` | `mineru:3.4.4-upstream`（服务器需已存在） |
| PPU 方式 A | [offline-package-ppu/README-ppu-deploy.md](../offline-package-ppu/README-ppu-deploy.md) | `offline-package-ppu/MANIFEST-ppu.txt` | `mineru:ppu-fork-3.4.4` |
| PPU 方式 B | [offline-package-ppu-mount/README-ppu-mount-deploy.md](../offline-package-ppu-mount/README-ppu-mount-deploy.md) | `offline-package-ppu-mount/MANIFEST-ppu-mount.txt` | `mineru:ppu-vllm-latest`（服务器需已存在） |

## 构建入口（构建机专用，不随包分发）

| 包 | 构建教程 | 脚本 |
|----|---------|------|
| NVIDIA 方式 A | `build-offline-package.sh` 内注释 | [build-offline-package.sh](build-offline-package.sh) |
| NVIDIA 方式 B | [offline-package-mount/README-mount-build.md](../offline-package-mount/README-mount-build.md) | `build-mount-src.sh` |
| PPU 方式 A | [offline-package-ppu/README-ppu-build.md](../offline-package-ppu/README-ppu-build.md) | `build-ppu-fork.sh` |
| PPU 方式 B | [offline-package-ppu-mount/README-ppu-mount-build.md](../offline-package-ppu-mount/README-ppu-mount-build.md) | `build-ppu-src.sh` |

## 方式 A vs 方式 B 选型

| | 方式 A（烤代码进镜像） | 方式 B（源码挂载） |
|---|---|---|
| 代码怎么进容器 | 打进镜像 | 挂载宿主机源码目录 |
| 改代码后 | 重打镜像 + `docker load` + 重建容器 | 换源码 + `restart` |
| 交付物 | 40GB 镜像 tar.gz（自包含） | 几 MB 源码包（依赖基础镜像已在） |
| 适合 | 稳定交付 / 首次部署 | 频繁更新（跟踪 develop） |

> **NVIDIA vs PPU 差异**：PPU 把模型**烤进镜像**（43GB，自包含）；NVIDIA 模型单独一个 tar.gz（约 4GB）运行时挂载。
