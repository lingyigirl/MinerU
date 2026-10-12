# MinerU 海光 BW1000（深算三号 DCU）方式 A —— 部署操作卡

> **面向对象**：内网运维 / 部署人员（**镜像已构建好，无需构建**）
> **预计耗时**：10-15 分钟
> **前提**：已收到物料包（见下「你的物料包里有什么」）

---

## 术语速查

| 名称 | 含义 |
|------|------|
| 海光 BW1000（深算三号） | 海光信息第三代 DCU 加速卡，64GB HBM2e，~480 TFLOPS FP16 |
| `/dev/hycu*` | BW1000 在 Linux 下的设备节点名（rock-6.3.x+ 驱动，内核模块 `hycu`） |
| `dcu-smi` | 海光 DCU 的加速卡状态查看工具（对应 `nvidia-smi` / `ppu-smi`） |
| DTK | DCU Toolkit（海光 DCU 的 CUDA 等价软件栈） |

**指定卡 vs 查看卡（别搞混）**：

- `dcu-smi`：只**查看**卡状态 / 哪张空闲（等价 `nvidia-smi`），**不指定**卡。
- `CUDA_VISIBLE_DEVICES`：**指定**用哪张卡（DCU 走类 CUDA 模拟层，用法和 NVIDIA 相同）。

流程：先 `dcu-smi` 看空闲卡号 → 填进 compose 里的 `CUDA_VISIBLE_DEVICES`（默认 `"0"`）。

---

## 你的物料包里有什么

```
mineru-bw1000-fork-3.4.4.tar.gz   ← Docker 镜像（约 40-50GB，含模型）
compose-bw1000.yaml               ← 启动配置
check_server_env.sh               ← 环境检查脚本
README-bw1000-deploy.md           ← 本文件
MANIFEST-bw1000.txt               ← 物料清单
```

> 本包是**方式 A**（代码烤进镜像，自包含）。模型已烤入镜像，无需额外挂载模型文件。

---

## 第一步：环境检查

```bash
cd /data/offline-package-bw1000
bash check_server_env.sh
```

**重点看**：
- DCU 设备节点（`/dev/hycu*` 等）是否存在 — FAIL 级，不存在则无法部署
- `dcu-smi` 是否能列出 DCU 卡
- 端口 8000 是否空闲
- 磁盘可用 ≥ 50GB

有 FAIL 先解决再继续。

---

## 第二步：导入 Docker 镜像

```bash
gunzip -c mineru-bw1000-fork-3.4.4.tar.gz | docker load
```

验证：
```bash
docker images mineru:bw1000-fork-3.4.4
# 应看到一行镜像信息（约 40-50GB）
```

---

## 第三步：修改 compose-bw1000.yaml

至少改 **2 处**：

1. **DCU 卡号**：`CUDA_VISIBLE_DEVICES: "0"` → 你的空闲卡号（`dcu-smi` 查看）
2. **输出目录**：默认 `/data/mineru_output`，按服务器实际路径修改

### 设备节点说明（选读）

compose 中的 `devices` 块预设了 `/dev/hycu0`。如果你服务器的 DCU 设备节点不同（因驱动版本差异），请对应修改：

| 驱动版本 | 内核模块 | 设备节点 |
|----------|---------|---------|
| rock-6.3.x+ | `hycu` | `/dev/hycu0`、`/dev/hycu1` ... |
| rock-6.2.x- | `hydcu` | `/dev/hydcu0`、`/dev/hydcu1` ... |
| 部分部署 | `amdgpu` | `/dev/dri/`（kfd 方式） |

如果不确定，在服务器上 `ls /dev/hycu* /dev/hydcu* /dev/dri/` 查看实际节点。

---

## 第四步：启动服务

```bash
# 创建输出目录
mkdir -p /data/mineru_output

# 启动
docker compose -f compose-bw1000.yaml up -d

# 查看日志（等 2-3 分钟直到出现 "Uvicorn running on..."）
docker compose -f compose-bw1000.yaml logs -f
```

按 `Ctrl+C` 退出日志查看，服务在后台继续运行。

如启动失败，常见原因排查见「遇到问题？」表。

---

## 第五步：验证

```bash
# 健康检查
curl http://localhost:8000/health
# 返回 {"status":"ok"} 表示正常

# 测试解析一个 PDF（必须传 backend=hybrid-transformers，详见下方说明）
curl -X POST http://localhost:8000/file_parse \
  -F "file=@/path/to/发票.pdf" \
  -F "backend=hybrid-transformers" \
  -o /tmp/test.zip
# 返回一个 zip 文件表示正常
```

> **⚠️ 为什么必须传 `backend=hybrid-transformers`？**
> 该服务的 sync 端点（`/file_parse`）对非 pipeline 后端内部走异步路径。默认值 `hybrid-auto-engine` 的 auto 引擎选择会选中 `vllm-async-engine`，该引擎需要 vLLM ≥ 0.10 的 `AsyncLLM` API，而海光 DCU 镜像的 vLLM 0.9.2+das 不支持。`hybrid-transformers` 显式指定 transformers 引擎，绕过 auto 选择，已在镜像内验证当前 VLM 模型（Qwen2VL 架构）加载推理均正常。
> 不需额外传 `-F "backend=..."` 时的默认值，原因同上——默认走 `vllm-async-engine` 会失败。

---

## 常用运维命令

| 操作 | 命令 |
|------|------|
| 启动服务 | `docker compose -f compose-bw1000.yaml up -d` |
| 查看状态 | `docker compose -f compose-bw1000.yaml ps` |
| 查看日志 | `docker compose -f compose-bw1000.yaml logs --tail 100` |
| 重启服务 | `docker compose -f compose-bw1000.yaml restart` |
| 停止服务 | `docker compose -f compose-bw1000.yaml down` |
| 查看 DCU 卡 | `dcu-smi` |

---

## 遇到问题？

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| DCU 卡不可用 | 设备未透传 / 卡号错 / 驱动未装 | 确认 `/dev/hycu*` 是否存在，`dcu-smi` 看卡号（再填进 `CUDA_VISIBLE_DEVICES`） |
| 健康检查失败 | 模型还在加载 | 再等 1-2 分钟 |
| 端口被占用 | 其他服务用了 8000 | 改 compose 中的端口映射 |
| 内存不足 | 系统内存 < 64GB | 增大 swap 或调小 `shm_size` |
| 解析结果为空 | PDF 损坏或有密码 | 换一个正常 PDF 测试 |
| 解析报缺 OriCls 模型 | 基础镜像构建时下载列表漏了该模型 | 确认基础镜像 `docker/china/bw1000.Dockerfile` 含 OriCls 补下载步骤；如缺，重跑步骤 1 |
| 内核驱动版本不匹配 | kernel-devel 与内核版本不一致 | 安装驱动时确保 `uname -r` 与 `kernel-devel` 版本一致 |

---

> **完整文档**：如需了解多 DCU 部署、性能调优等高级内容，请参考 `deploy/mineru-3.4.4-offline-deployment-guide.md`