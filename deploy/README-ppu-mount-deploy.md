# MinerU PPU（平头哥 T-Head）源码挂载更新操作卡

> **适用场景**：已有一台 **T-Head PPU 加速卡**（`/dev/alixpu`，非 NVIDIA）内网服务器，跑的是旧版 mineru，现在要把 fork 的 release3.4.4（含表格/发票后处理优化）更新上去。
> **核心思路**：PPU 基础镜像提供 vLLM 运行时 + 依赖，fork 源码通过挂载注入，改代码只 `rsync + restart`，不重打镜像。

---

## 一、为什么 PPU 部署这么复杂

PPU 是**非 NVIDIA 加速卡**（平头哥 ZW810E），跑 MinerU 需要一套与 NVIDIA 完全不同的适配：

| 项 | NVIDIA | PPU |
|---|---|---|
| 基础镜像 | `vllm/vllm-openai:v0.11.2` | PPU 专用 `ppu:...-vllm0.8.5` |
| 设备透传 | `--gpus all`（nvidia-container-toolkit） | `--device=/dev/alixpu` |
| 共享内存 | 默认 | `--shm-size=500g` |
| 权限 | 普通 | `--privileged` |
| 卡选择 | `CUDA_VISIBLE_DEVICES` | `ppu-smi` + `CUDA_VISIBLE_DEVICES`（模拟 CUDA） |

这些是 PPU 硬件适配的固有要求，不是 MinerU 引入的。NVIDIA 之所以简单，是因为它有 Docker 一等公民支持。

---

## 二、前提

- 内网服务器有 T-Head PPU 卡，`ppu-smi` 能识别卡号。
- 已有（或能构建）PPU 基础镜像 `mineru:ppu-vllm-latest`。
- 有 fork 的 `mineru/` 源码（rsync 或 git clone develop/release3.4.4）。

---

## 三、一次性：构建 PPU 基础镜像

fork 仓库里已有 PPU 专用 Dockerfile：`docker/china/ppu.Dockerfile`。

```bash
# 在线构建（需能访问阿里云镜像源）
docker build --network=host -t mineru:ppu-vllm-latest -f docker/china/ppu.Dockerfile .
# 离线则从已有服务器 docker save/load 镜像
```

> 注意：该 Dockerfile 装的是**上游 PyPI 的 `mineru[core]>=3.0.0`**（不含 fork 自定义代码），fork 的优化靠下面的源码挂载注入。

---

## 四、挂载 fork 源码（方式 B 核心）

```bash
# 1. 放 fork 源码到宿主机
mkdir -p /data/mineru-src
rsync -avz --delete 开发机:/zhangbo/MinerU/mineru/ /data/mineru-src/mineru/

# 2. 确认 PPU 镜像内 mineru 安装路径（用于挂载目标）
docker run --rm mineru:ppu-vllm-latest python -c "import mineru; print(mineru.__file__)"
# 一般输出 .../dist-packages/mineru/__init__.py，挂载其父目录
```

修改 `compose-mount-ppu.yaml` 里的源码挂载路径，与上面输出一致。

---

## 五、依赖说明（已核实）

fork 的表格/发票后处理（`mineru/utils/custom/table_utils.py`、`title_utils.py`、`seal_utils.py`）**只用到** `bs4`、`loguru`、`PIL`、`cv2`、`numpy`——这些上游 `mineru[core]` **已经安装**，所以挂载源码即可，**无需额外 pip 补装**。

> 只有当你还要用 fork 的 office（docx/pptx/xlsx）或 KVP（PaddleOCR）功能时，才需要补装 `pandas`/`openpyxl`/`paddleocr` 等额外依赖。

---

## 六、启动 + 验证

```bash
mkdir -p /data/mineru_output
docker compose -f compose-mount-ppu.yaml up -d
docker compose -f compose-mount-ppu.yaml logs -f   # 等出现 "Uvicorn running on..."
```

```bash
# 健康检查（host 网络，直接宿主机 8000）
curl http://localhost:8000/health

# 传发票 PDF 验证表格后处理生效
curl -X POST http://localhost:8000/file_parse \
  -F "files=@/path/to/发票.pdf" \
  -F "backend=vlm-vllm-async-engine" \
  -o /tmp/test.zip
```

---

## 七、日常更新代码（无需重打镜像）

```bash
# 1. 同步 fork 最新源码
rsync -avz --delete 开发机:/zhangbo/MinerU/mineru/ /data/mineru-src/mineru/

# 2. 重启容器
docker compose -f compose-mount-ppu.yaml restart

# 3. 确认
curl http://localhost:8000/health
```

---

## 八、与 NVIDIA 方式的关系

| | 方式 A/B（NVIDIA） | 方式 B（PPU） |
|---|---|---|
| 基础镜像 | `mineru:3.4.4-upstream` | `mineru:ppu-vllm-latest` |
| 适用硬件 | NVIDIA GPU | T-Head PPU |
| 源码挂载思路 | ✅ 相同 | ✅ 相同 |
| 镜像能否互换 | ❌ 不能（CUDA vs PPU） | ❌ 不能 |

两者是**两套互不兼容的镜像**，只是"源码挂载覆盖代码"这个思路通用。

---

## 九、常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 改的代码没生效 | 源码路径没挂对 / 没 restart | 确认挂载路径 = `import mineru` 输出，`restart` |
| 卡不可用 | 设备未透传 / 卡号错 | 确认 `/dev/alixpu` 存在，`ppu-smi` 看卡号 |
| 报 `vllm` 找不到 | 用了 NVIDIA 镜像 | 确认 image 是 `mineru:ppu-vllm-latest` |
| ModuleNotFoundError | 新文件没同步 | `rsync --delete` 确保新增文件也同步 |
