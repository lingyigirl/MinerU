# MinerU 镜像与容器清单（误删恢复手册）

> 记录于 2026-09-10，构建机 `/zhangbo/MinerU`（release3.4.4）。
> 目的：**任何镜像被误删后，都能按本文重建、恢复部署。**

---

## 一、总览

| # | 镜像 | Image ID | 大小 | 构建日期 | 代码安装方式 | 模型 | 用途 / 状态 |
|---|------|----------|------|----------|--------------|------|-------------|
| 1 | `mineru:3.4.4` | `057a5e802f35` | 29.7 GB | 2026-07-06 | `pip install -e .[core]` 可编辑 → `/opt/mineru/mineru` | ❌ 无（外部挂载） | **生产在用**：8010 + 8011 容器 |
| 2 | `mineru:2.7.6` | `3082c9d46463` | 34.7 GB | 2026-04-01 | 常规安装 + `mineru-models-download -m all` | ✅ 内置 | 旧版（旧 3.2.0 环境），无容器 → 可删 |
| 3 | `mineru-ppu:3.0.2` | `51b42d699c3e` | 43.5 GB | 2026-04-22 | 常规 `mineru[core]==3.0.0` + `COPY ./mineru` | ✅ 内置 | 旧 PPU 版，无容器 → 可删 |
| 4 | `mineru:3.4.4-upstream` | `acba1db61e51` | 29.6 GB | 2026-08-03 | `pip install -e .[core]` 可编辑 → `/opt/mineru/mineru` | ❌ 无（外部挂载） | NVIDIA 离线包候选镜像，无容器 |
| 5 | `mineru:ppu-vllm-latest` | `95c271f988ee` | 43.1 GB | 2026-08-18 | 常规 `mineru[core]>=3.0.0` + 模型 | ✅ 内置 | ⛔ **已于 2026-09-10 删除**（无 tar.gz，需联网重建） |
| 6 | `mineru:ppu-fork-3.4.4` | `e44bb4fd6845` | 43.1 GB | 2026-09-10 | 从 ⑤ 薄层 COPY fork 代码 + OriCls | ✅ 继承 | ⛔ **已于 2026-09-10 删除**（交付 tar.gz 已丢失） |

> 相关基础镜像（非 mineru 命名，勿删）：`vllm/vllm-openai:v0.11.2`（NVIDIA 构建的 FROM 基础）。

---

## 二、当前运行的 MinerU 服务

两个容器**都使用镜像 `mineru:3.4.4`**（Image ID `057a5e802f35`）。

### 8010 — `mineru-api2`

| 项 | 值 |
|----|----|
| 容器名 | `mineru-api2` |
| 镜像 | `mineru:3.4.4` |
| 端口 | `8010:8000` |
| compose | `/root/project/mineru_3.4.4/compose.yaml` |
| 项目名 | `mineru_344` |
| GPU | `device_ids: ["2"]` |
| 启动 | `2026-08-07` |

挂载：

```
/root/project/mineru_3.4.4/mineru    -> /opt/mineru/mineru            # ✅ 源码（生效）
/root/project/mineru_3.4.4/mineru.json -> /root/mineru.json
/root/output                          -> /root/project/docker-file
/root/project/mineru_3.4.4/data       -> /vllm-workspace/output
/zhangbo/mineru_models                -> /root/.cache/modelscope      # 模型
```

### 8011 — `mineru-zhangbo`

| 项 | 值 |
|----|----|
| 容器名 | `mineru-zhangbo` |
| 镜像 | `mineru:3.4.4` |
| 端口 | `8011:8000` |
| compose | `/zhangbo/MinerU/compose.yaml` |
| 项目名 | `mineru` |
| GPU | `device_ids: ["2"]` |
| 启动 | `2026-08-12` |

挂载：

```
/zhangbo/MinerU/mineru    -> /usr/local/lib/python3.12/dist-packages/mineru   # ⚠️ 源码（不生效，见第四节）
/zhangbo/MinerU/mineru.json -> /root/mineru.json
/root/output                -> /root/project/docker-file
/zhangbo/MinerU/data        -> /vllm-workspace/output
/zhangbo/mineru_models      -> /root/.cache/modelscope                        # 模型
```

### 模型来源

两个容器的 `mineru.json` 都指向宿主机挂载目录，模型**不在镜像里**：

```json
"models-dir": {
  "pipeline": "/root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",
  "vlm":      "/root/.cache/modelscope/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"
}
```

对应宿主目录 `/zhangbo/mineru_models`（挂到容器 `/root/.cache/modelscope`）。

---

## 三、镜像详细说明与重建方法

### ① `mineru:3.4.4` —— 生产镜像（**不可删**）

- **用途**：8010 / 8011 两个生产容器正在使用。
- **特征**：`pip install -e .[core]` **可编辑安装**，代码在 `/opt/mineru/mineru`，通过 `__editable__.mineru-*.pth` finder 导入（**`dist-packages/mineru` 目录不存在**）。
- **模型**：无，运行时靠挂载 `/root/.cache/modelscope`。
- **重建**：

```bash
cd /zhangbo/MinerU
docker build -t mineru:3.4.4 -f Dockerfile .
# 或国内镜像变体：
docker build -t mineru:3.4.4 -f docker/china/Dockerfile .
```

> 重建后需 `docker compose -f /root/project/mineru_3.4.4/compose.yaml up -d`
> 与 `docker compose -f /zhangbo/MinerU/compose.yaml up -d` 重启两个容器。

---

### ② `mineru:2.7.6` —— 旧版 NVIDIA（无容器，可删）

- **用途**：旧 `/root/project/mineru_3.2.0/compose.yaml` 使用的镜像；该环境已被 `mineru_3.4.4` 取代（同名容器 `mineru-api2` 已被 8010 占用）。
- **特征**：镜像内已 `mineru-models-download -s modelscope -m all`，entrypoint 为
  `bash -c "export MINERU_MODEL_SOURCE=local && exec \"$@\""`。
- **删除**：

```bash
docker rmi mineru:2.7.6
```

- **若日后要重建旧 3.2.0 环境**：不必还原此镜像，直接用新版镜像 + 挂载代码即可：
  1. 构建 `mineru:3.4.4`（见 ①）；
  2. 把 `/root/project/mineru_3.2.0/compose.yaml` 的 `image:` 改成 `mineru:3.4.4`；
  3. **同时把源码挂载路径改为 `/opt/mineru/mineru`**（旧 compose 写的是
     `/usr/local/lib/python3.12/dist-packages/mineru`，对可编辑安装无效）。

---

### ③ `mineru-ppu:3.0.2` —— 旧 PPU（无容器，可删）

- **用途**：早期 PPU 部署镜像，已被 `mineru:ppu-fork-3.4.4` 取代。
- **特征**：**常规安装**，代码在 `/usr/local/lib/python3.12/site-packages/mineru`
  （构建记录：`pip install 'mineru[core]==3.0.0'` + `COPY ./mineru`），模型内置。
- **删除**：

```bash
docker rmi mineru-ppu:3.0.2
```

- **重建**：见 ⑥ 的 PPU 构建流程（用当前 3.4.4 版本，比 3.0.2 新）。

---

### ④ `mineru:3.4.4-upstream` —— NVIDIA 离线包候选（无容器）

- **用途**：`offline-package/` NVIDIA 离线部署的镜像产物；与 ① 同为可编辑安装、无内置模型。
- **特征**：`pip install -e .[core]` → `/opt/mineru/mineru`。
- **重建**（自包含方式 A，模型烤入）：

```bash
cd /zhangbo/MinerU
bash offline-package/build-nvidia.sh          # 产物: offline-package/mineru-3.4.4-upstream.tar.gz
# 等价手工命令：
docker buildx build --load \
  --build-context models=/zhangbo/mineru_models \
  -f offline-package/nvidia-fork.Dockerfile \
  -t mineru:3.4.4-upstream .
```

---

### ⑤ `mineru:ppu-vllm-latest` —— PPU 构建基础镜像 ⛔ **已删除（2026-09-10）**

> **状态**：镜像与标签已于 2026-09-10 删除，本地无任何 tar.gz 备份。
> 恢复必须联网重建（下方命令），无法从本地文件还原。
> 层仍被悬空镜像 `7d79d59d9ad3` 引用，需一并清理才能回收磁盘（见第七节）。

- **用途**：`offline-package-ppu/ppu-fork.Dockerfile` 的 `FROM`。
- **特征**：由 `docker/china/ppu.Dockerfile` 生成，常规安装 `mineru[core]>=3.0.0` + 内置 modelscope 模型
  （代码在 `/usr/local/lib/python3.12/site-packages/mineru`）。
- **重建**（需联网 + PPU ACR 源）：

```bash
cd /zhangbo/MinerU
docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .
```

---

### ⑥ `mineru:ppu-fork-3.4.4` —— PPU 交付镜像（方式A）⛔ **已删除（2026-09-10）**

> **状态**：镜像与标签已于 2026-09-10 删除。
> ⚠️ 本地 tar.gz `mineru-ppu-fork-3.4.4.tar.gz` **不存在**（8月18日导出过一个 41G 的版本，但文件已被删除/转移）。
> 若内网 PPU 服务器上已 `docker load` 过旧版 tar.gz，则服务不受影响；
> 若未交付过，需按上述重建流程重做（联网重建基础镜像 → 重建 fork 镜像 → 导出）。

- **用途**：交付到内网 PPU 服务器的自包含镜像。
- **特征**：`FROM mineru:ppu-vllm-latest`，薄层 `COPY mineru/ /opt/mineru-fork/` 后
  `rmtree` 替换 `/usr/local/lib/python3.12/site-packages/mineru`，并补齐 **OriCls** 模型。
- **重建**：

```bash
cd /zhangbo/MinerU
# 先重建基础镜像（联网）
docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .
# 再重建 fork 镜像
bash offline-package-ppu/build-ppu-fork.sh
```

---

## 四、源码挂载路径解析（附一次误判的更正）

> ✅ **正确结论：8011 与 8010 的源码挂载都生效，两个服务跑的都是 fork 代码（3.4.4）。**
>
> ⚠️ **更正**：2026-09-10 本文件曾记录"8011 挂载失效、运行烘焙代码 4.2.0"，
> 该结论**错误**，原因是排查时 `docker exec` 未加 `-w`，继承了容器 `WORKDIR=/opt/mineru`，
> 导致 `sys.path[0]=''` 命中烘焙目录。详见下方"调试陷阱"。

### 为什么两种挂载路径都能生效

镜像 `mineru:3.4.4` 是可编辑安装，其 `.pth` 里的 finder 是**追加**到 `sys.meta_path` 末尾的：

```python
# /usr/local/lib/python3.12/dist-packages/__editable___mineru_4_2_0_finder.py
if not any(finder == _EditableFinder for finder in sys.meta_path):
    sys.meta_path.append(_EditableFinder)          # append → 排在 PathFinder 之后
```

`PathFinder` 先于它被调用，所以**只要 `sys.path` 里存在 `mineru` 包，就直接命中，轮不到 editable finder**：

| 容器 | 挂载源 | 挂到容器内 | 解析路径 | 是否生效 |
|------|--------|-----------|----------|----------|
| **8010** | `/root/project/mineru_3.4.4/mineru` | `/opt/mineru/mineru` | dist-packages 无 `mineru` → 落到 editable finder → `/opt/mineru/mineru`（=挂载源） | ✅ |
| **8011** | `/zhangbo/MinerU/mineru` | `/usr/local/lib/python3.12/dist-packages/mineru` | `dist-packages` 在 `sys.path` 上 → PathFinder 直接命中挂载路径 | ✅ |

两条路径机制不同，**结果都是挂载的 fork 代码**。

### ⚠️ 调试陷阱：`docker exec` 必须带 `-w`

容器 `WORKDIR=/opt/mineru`。`docker exec ... python3 -c` 不加 `-w` 时 `sys.path[0]=''`（=cwd），
会**先命中烘焙的 `/opt/mineru/mineru`**，得到与真实服务相反的结论：

```bash
# ❌ 误导：cwd 继承 /opt/mineru
docker exec mineru-zhangbo python3 -c "import mineru; print(mineru.__file__)"
# → /opt/mineru/mineru/__init__.py        （烘焙代码）

# ✅ 正确：模拟真实服务（console script 的 sys.path[0] 是脚本目录）
docker exec -w / mineru-zhangbo python3 -c "import mineru; print(mineru.__file__)"
# → /usr/local/lib/python3.12/dist-packages/mineru/__init__.py   （挂载的 fork）

docker exec -w /usr/local/bin mineru-zhangbo python3 -c \
  "import mineru,os;print(open(os.path.join(os.path.dirname(mineru.__file__),'version.py')).read().strip())"
# → __version__ = "3.4.4"
```

### 运行实证

一次 8011 的 `POST /file_parse`（任务 `9ab27586-430f-47e5-996a-70c51ddf6b17`）产出了
`交易奥义非农_90_content_list_compatibility.json`（v1 扁平结构 + `v2_type`/`v2_content` 增强字段）。
该文件名与结构**只存在于 fork 代码**——烘焙代码全树搜索 `ompatibilit` 仅有 4 处无关匹配，
根本没有 `CONTENT_LIST_COMPATIBILITY` 符号。**故 8011 确实在跑 fork 代码。**

日志同样佐证：

```
2026-09-10 22:46:35 | INFO | mineru.utils.custom.pdf_utils:generate_rotation_corrected_pdf:138
  - 旋转修正PDF——文档多数派方向=270，逐页判定=[(1,'270',0.451),(2,'270',0.451)]
```

> 注：`generate_rotation_corrected_pdf` 在烘焙代码与 fork 中都存在，
> 因此"旋转能用"**不能**用来区分二者；真正的判别依据是 `content_list_compatibility` 这类 fork 专有符号。

**结论**：`/zhangbo/MinerU` 的代码改动**会**在重启 8011 后生效，无需修改 compose。

---

## 五、镜像路径速查表

| 镜像 | 安装方式 | 源码目录（容器内） |
|------|----------|-------------------|
| `mineru:3.4.4`（生产用） / `mineru:3.4.4-upstream` | 可编辑 `pip install -e` | `/opt/mineru/mineru`（editable 映射） |
| `mineru:2.7.6`（已删） | 常规 | site-packages |
| PPU 系列（已删） | 常规 | `/usr/local/lib/python3.12/site-packages/mineru` |

### 挂载源码时的路径选择

**可编辑安装的镜像（`mineru:3.4.4`）有两条可用路径**（见第四节原理）：

| 挂到容器内 | 原理 | 实例 |
|-----------|------|------|
| `/opt/mineru/mineru` | 覆盖 editable 映射目录 | 8010 |
| `/usr/local/lib/python3.12/dist-packages/mineru` | 利用 PathFinder 先于 editable finder 命中 | 8011 |

**常规安装的镜像（PPU 系列）只能挂** `/usr/local/lib/python3.12/site-packages/mineru`。

> ⚠️ 排查时 `docker exec` 务必带 `-w`，否则会得到相反结论（见第四节"调试陷阱"）。

---

## 六、删除与恢复

### 安全删除（不影响生产）

```bash
docker rmi mineru:2.7.6          # 34.7 GB，旧版
docker rmi mineru-ppu:3.0.2      # 43.5 GB，旧版
```

### 不可删除（2026-09-10 更新）

```bash
# docker rmi mineru:3.4.4            # ❌ 8010 + 8011 正在使用
# docker rmi mineru:3.4.4-upstream    # ✅ 可删（非生产用，纯离线包候选，无容器引用）
```

### 已删除（2026-09-10 执行）

```bash
docker rmi mineru:2.7.6              # 2026-09-10 ✅ 旧版 NVIDIA
docker rmi mineru-ppu:3.0.2          # 2026-09-10 ✅ 旧版 PPU
docker rmi mineru:ppu-vllm-latest    # 2026-09-10  PPU 基础（重建需联网）
docker rmi mineru:ppu-fork-3.4.4     # 2026-09-10  PPU 交付（tar.gz 已丢失）
```

> ⚠️ 删除时有两组悬空镜像未清理（见第七节），继续保留 `7d79d59d9ad3`（43GB）和 `c704c24974ce`（30GB），
> 保持 PPU 基础层可被引用；若需要完全回收磁盘空间，可额外清理。

### 全部镜像一键重建顺序

```bash
cd /zhangbo/MinerU

# 1) 生产镜像（8010/8011）
docker build -t mineru:3.4.4 -f Dockerfile .

# 2) NVIDIA 离线包（方式A，自包含）
bash offline-package/build-nvidia.sh

# 3) PPU 基础镜像（联网）
docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .

# 4) PPU 交付镜像（方式A，自包含）
bash offline-package-ppu/build-ppu-fork.sh
```

### 从 tar.gz 恢复

```bash
docker load -i offline-package-ppu/mineru-ppu-fork-3.4.4.tar.gz
docker load -i offline-package/mineru-3.4.4-upstream.tar.gz
```

---

### 磁盘现状（2026-09-10 清理后）

```
Images      27 个，共 177.7 GB，可回收 95.4 GB
Containers  22 个，共 991.6 MB
Build Cache 220 条，34.47 GB（全部可回收）
宿主机 /var/lib/docker：3.5T，已用 392G，可用 2.9T
```

**悬空镜像（可回收的大头）**：

| Image ID | 大小 | 说明 | 引用容器 |
|----------|------|------|----------|
| `7d79d59d9ad3` | 43.1 GB | 8月18日旧 PPU fork 镜像 | `pedantic_kilby`（已退出） |
| `c704c24974ce` | 29.7 GB | 旧 `mineru:3.4.4` 构建残留 | 无 |
| `563c74d9120d` | 338 MB | 构建残留 | 无 |

清理命令（**确认无用途后执行**）：

```bash
docker rm pedantic_kilby
docker rmi 7d79d59d9ad3 c704c24974ce 563c74d9120d
docker builder prune -f          # 再回收 ~34 GB 构建缓存
```
