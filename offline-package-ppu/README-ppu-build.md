# MinerU PPU（平头哥 T-Head）方式 A —— 构建教程（构建机专用）

> **面向对象**：构建 / 开发人员（在**联网构建机**上操作）
> **本文件只在构建机用，不随包分发到服务器**。部署人员看 `README-ppu-deploy.md`。
> **产出**：`mineru-ppu-fork-3.4.4.tar.gz`（fork 代码烤进 PPU 镜像的离线包）

---

## 前置

- 构建机有 docker（联网），需拉上海基座 + pip 装 mineru + modelscope 下模型。
- 基座镜像**公开可拉、无需 `docker login`**（`docker/china/ppu.Dockerfile` 的 FROM 是上海 ACR 官方 `opendatalab-mineru/ppu`）。**不要改 FROM 指向杭州**——杭州账号下无该命名空间。

## 步骤 1：重建基础镜像（一次性，耗时较长）

```bash
cd <仓库根目录>   # 例如 /zhangbo/MinerU
docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .
```

> 该步联网 `pip install 'mineru[core]>=3.0.0'`（解析到 3.4.4）+ `mineru-models-download` 下模型，耗时十几分钟~几十分钟。基础镜像重建过一次后，后续更新只需重跑步骤 2。

> ⚠️ **不能用服务器旧镜像做 FROM**（如 2 月 `mineru0210.tar`）：薄层 COPY 只换代码不换依赖，旧镜像缺 `pdftext`/`magika`/`mineru-vl-utils`，烤出来仍会 `import` 报错。

## 步骤 2：薄层 COPY fork 源码 + 导出 tar.gz

```bash
bash offline-package-ppu/build-ppu-fork.sh
# 产物：offline-package-ppu/mineru-ppu-fork-3.4.4.tar.gz
```

> `build-ppu-fork.sh` 会先检查基础镜像 `mineru:ppu-vllm-latest` 是否存在，缺了就报错提醒先跑步骤 1。

验证 fork 代码已烤入：

```bash
docker run --rm mineru:ppu-fork-3.4.4 python3 -c "import mineru.utils.custom.table_utils; print('ok')"
```

## 更新代码（改代码后重打）

- 只改 `mineru/` 源码 → 重跑**步骤 2**（`build-ppu-fork.sh`）。
- 改了 `pyproject.toml` 依赖 → 重跑**步骤 1 + 2**（重建基础镜像）。

## 交付清单（发给部署人员）

把下面文件发给部署人员（放到服务器 `/data/` 下）：

```
mineru-ppu-fork-3.4.4.tar.gz   ← 镜像（docker load 用）
compose-ppu.yaml               ← 启动配置
check_server_env.sh            ← 环境检查脚本
README-ppu-deploy.md           ← 部署教程（部署人员读这个）
MANIFEST-ppu.txt               ← 物料清单
```
