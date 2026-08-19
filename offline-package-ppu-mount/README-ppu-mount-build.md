# MinerU PPU（平头哥 T-Head）方式 B —— 打包源码教程（构建机专用）

> **面向对象**：构建 / 开发人员（在开发机上操作）
> **本文件只在构建机用，不随包分发到服务器**。部署人员看 `README-ppu-mount-deploy.md`。
> **产出**：`mineru-src-3.4.4.tar.gz`（fork 源码包，部署人员解压后挂载）

---

## 步骤：打包 fork 源码

```bash
cd <仓库根目录>   # 例如 /zhangbo/MinerU
bash offline-package-ppu-mount/build-ppu-src.sh
# 产物：offline-package-ppu-mount/mineru-src-3.4.4.tar.gz
```

> 脚本自动定位仓库根目录，排除 `__pycache__`/`.git`。

## 更新代码（改代码后重打包）

改 `mineru/` 源码后，重跑上面命令重新打包源码，把新的 `mineru-src-3.4.4.tar.gz` 发给部署人员。

> 改了 `pyproject.toml` 依赖 → 方式 B 挂载不生效，需回到方式 A 重建镜像（见 `../offline-package-ppu/README-ppu-build.md`）。

## 在线备选（开发机与服务器可达时）

不用源码包，直接 rsync 同步源码到服务器：

```bash
mkdir -p /data/mineru-src
rsync -avz --delete 开发机:/zhangbo/MinerU/mineru/ /data/mineru-src/mineru/
# 更新时重复上面一条 → docker compose -f compose-mount-ppu.yaml restart
```

## 交付清单（发给部署人员）

```
mineru-src-3.4.4.tar.gz       ← 源码包（解压后挂载）
compose-mount-ppu.yaml        ← 启动配置（含源码挂载）
check_server_env.sh           ← 环境检查脚本
README-ppu-mount-deploy.md    ← 部署教程（部署人员读这个）
MANIFEST-ppu-mount.txt        ← 物料清单
```

> 前提：服务器基础镜像 `mineru:ppu-vllm-latest` 内 mineru 已是 3.4.4（否则缺依赖，改用方式 A）。
