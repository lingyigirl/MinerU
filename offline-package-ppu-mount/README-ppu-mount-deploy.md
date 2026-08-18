# MinerU PPU（平头哥 T-Head）方式 B —— 源码挂载部署操作卡

> **面向对象**：内网运维 / 部署人员（源码已打包好，无需构建）
> **预计耗时**：10-15 分钟
> **前提**：服务器基础镜像 `mineru:ppu-vllm-latest` 内 mineru **已是 3.4.4**；已收到物料包

---

## 术语速查（T-Head / alixpu / ppu-smi 是同一个硬件）

| 名字 | 含义 |
|---|---|
| T-Head（平头哥） | 阿里芯片子公司，这块 PPU 卡的厂商 / 平台名 |
| `/dev/alixpu` | 这块卡在 Linux 下的设备节点名（ali=阿里，xpu=加速器） |
| `ppu-smi` | T-Head 平台的加速卡状态查看工具（对应 `nvidia-smi`） |

**指定卡 vs 查看卡**：`ppu-smi` 只**查看**卡状态 / 哪张空闲；`CUDA_VISIBLE_DEVICES` **指定**用哪张卡（PPU 走 CUDA 模拟层）。流程：先 `ppu-smi` 看空闲卡号 → 填进 compose 的 `CUDA_VISIBLE_DEVICES`。

---

## 你的物料包里有什么

```
mineru-src-3.4.4.tar.gz       ← 源码包（解压后挂载）
compose-mount-ppu.yaml        ← 启动配置（含源码挂载）
check_server_env.sh           ← 环境检查脚本
README-ppu-mount-deploy.md    ← 本文件
MANIFEST-ppu-mount.txt        ← 物料清单
```

> 方式 A（烤代码进镜像）见 `../offline-package-ppu/README-ppu-deploy.md`。

---

## 第一步：环境检查（先做，必做）

```bash
cd /data/offline-package-ppu-mount
bash check_server_env.sh
```

**重点看两点**：

1. **PPU 卡**：`/dev/alixpu`、`/dev/alixpu_ctl` 是否 FAIL（不存在则无法部署）。
2. **基础镜像版本**（脚本第 5 项）：`mineru:ppu-vllm-latest` 内 mineru 版本
   - `= 3.4.4` → ✅ 继续。
   - `≠ 3.4.4`（如 2 月 `mineru0210.tar`）→ ⚠️ **改用方式 A**（挂载只换代码不换依赖，旧镜像缺 `pdftext`/`magika`/`mineru-vl-utils`，会 `import` 报错）。

> 为什么？方式 B 挂载只替换「代码」，不替换「依赖」。旧镜像里 mineru 是旧版，develop 3.4.4 新增依赖缺失，挂载后直接报错。

---

## 第二步：解压源码

```bash
mkdir -p /data/mineru-src
tar xzf /data/mineru-src-3.4.4.tar.gz -C /data/mineru-src
# 得到 /data/mineru-src/mineru/，正好是 compose 挂载源
```

---

## 第三步：确认挂载目标路径

```bash
docker run --rm mineru:ppu-vllm-latest python3 -c "import mineru; print(mineru.__file__)"
# 一般输出 .../dist-packages/mineru/__init__.py，父目录即挂载目标
```

---

## 第四步：改 compose-mount-ppu.yaml（3 处）

1. `CUDA_VISIBLE_DEVICES` → 卡号（`ppu-smi` 看）
2. 源码挂载左半边 → 与第二步解压路径一致（默认 `/data/mineru-src/mineru`）
3. `/mnt`、`/datapool` → 服务器实际路径

---

## 第五步：启动

```bash
mkdir -p /datapool/mineru_output
docker compose -f /data/compose-mount-ppu.yaml up -d
docker compose -f /data/compose-mount-ppu.yaml logs -f   # 等出现 "Uvicorn running on..."
```

**验证**：

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/file_parse \
  -F "files=@/path/to/发票.pdf" \
  -F "backend=vlm-vllm-async-engine" \
  -o /tmp/test.zip
```

> ⚠️ **端口待确认**：旧文档 V2.txt 用 `172.19.0.3:8009`，本 compose 是 `--port 8000` + host 网络。到服务器确认：`ss -tlnp | grep 800`。

---

## 日常更新代码（无需重打镜像）

构建人员改代码后会给你一个新的 `mineru-src-3.4.4.tar.gz`，你重新解压覆盖 + restart：

```bash
tar xzf /data/mineru-src-3.4.4.tar.gz -C /data/mineru-src
docker compose -f /data/compose-mount-ppu.yaml restart
curl http://localhost:8000/health
```

---

## 依赖说明（挂载为何能工作）

fork 的表格/发票后处理（`mineru/utils/custom/table_utils.py`、`title_utils.py`、`seal_utils.py`）**只用到** `bs4`、`loguru`、`PIL`、`cv2`、`numpy`——上游 `mineru[core]` **已装**，挂载源码即可，**无需额外 pip 补装**。

---

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 改的代码没生效 | 源码路径没挂对 / 没 restart | 确认挂载路径 = `import mineru` 输出父目录，`restart` |
| 卡不可用 | 设备未透传 / 卡号错 | 确认 `/dev/alixpu` 存在，`ppu-smi` 看卡号 |
| 报 `vllm` 找不到 | 用了 NVIDIA 镜像 | 确认 image 是 `mineru:ppu-vllm-latest` |
| ModuleNotFoundError | 新文件没同步 | 重新解压源码包（`tar xzf` 会覆盖） |
