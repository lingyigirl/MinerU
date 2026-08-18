# MinerU 3.4.4 源码挂载模式 —— 打包源码教程（方式 B，构建机专用）

> **面向对象**：构建 / 开发人员（在开发机上操作）
> **本文件只在构建机用，不随包分发到服务器**。部署人员看 `README-mount-deploy.md`。
> **产出**：`mineru-src-3.4.4.tar.gz`（fork 源码包，部署人员解压后挂载）

---

## 步骤：打包 fork 源码

```bash
cd <仓库根目录>   # 例如 /zhangbo/MinerU
bash offline-package-mount/build-mount-src.sh
# 产物：offline-package-mount/mineru-src-3.4.4.tar.gz
```

> 脚本自动定位仓库根目录，排除 `__pycache__`/`.git`。

## 更新代码（改代码后重打包）

改 `mineru/` 源码后，重跑上面命令重新打包，把新的 `mineru-src-3.4.4.tar.gz` 发给部署人员。

> 改了 `pyproject.toml` 依赖 → 方式 B 挂载不生效，需回到方式 A 重建镜像（见 `deploy/build-offline-package.sh`）。

## 在线备选（开发机与服务器可达时）

不用源码包，直接 rsync 同步源码到服务器：

```bash
mkdir -p /data/mineru-src
rsync -avz --delete 开发机:/zhangbo/MinerU/mineru/ /data/mineru-src/mineru/
# 更新时重复上面一条 → docker compose -f compose-mount.yaml restart
```

## 交付清单（发给部署人员）

```
mineru-src-3.4.4.tar.gz       ← 源码包（解压后挂载）
compose-mount.yaml            ← 启动配置（含源码挂载）
mineru-prod-template.json     ← 配置文件模板
check_server_env.sh           ← 环境检查脚本
README-mount-deploy.md        ← 部署教程（部署人员读这个）
MANIFEST-mount.txt            ← 物料清单
```

> 前提：服务器基础镜像 `mineru:3.4.4-upstream` 已在（方式 A 部署过）。
