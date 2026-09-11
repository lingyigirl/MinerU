# MinerU PPU（平头哥 T-Head）方式 A —— 部署操作卡

> **面向对象**：内网运维 / 部署人员（**镜像已构建好，无需构建**）
> **预计耗时**：10-15 分钟
> **前提**：已收到物料包（见下「你的物料包里有什么」）

---

## 术语速查（T-Head / alixpu / ppu-smi 是同一个硬件）

| 名字             | 含义                                                  |
| ---------------- | ----------------------------------------------------- |
| T-Head（平头哥） | 阿里芯片子公司，这块 PPU 卡的厂商 / 平台名            |
| `/dev/alixpu`  | 这块卡在 Linux 下的设备节点名（ali=阿里，xpu=加速器） |
| `ppu-smi`      | T-Head 平台的加速卡状态查看工具（对应`nvidia-smi`） |

**指定卡 vs 查看卡（别搞混）**：

- `ppu-smi`：只**查看**卡状态 / 哪张空闲（等价 `nvidia-smi`），**不指定**卡。
- `CUDA_VISIBLE_DEVICES`：**指定**用哪张卡（PPU 走 CUDA 模拟层，和 NVIDIA 用法相同）。

流程：先 `ppu-smi` 看空闲卡号 → 填进 compose 里的 `CUDA_VISIBLE_DEVICES`（默认 `"0"`）。

---

## 你的物料包里有什么

```
mineru-ppu-fork-3.4.4.tar.gz   ← Docker 镜像（约 40GB）
compose-ppu.yaml               ← 启动配置
check_server_env.sh            ← 环境检查脚本
README-ppu-deploy.md           ← 本文件
MANIFEST-ppu.txt               ← 物料清单
```

> 本包是**方式 A**（代码烤进镜像）。方式 B（源码挂载）见 `../offline-package-ppu-mount/README-ppu-mount-deploy.md`。

---

## 第一步：环境检查

```bash
cd /data/offline-package-ppu
bash check_server_env.sh
```

**重点看**：`/dev/alixpu`、`/dev/alixpu_ctl` 是否存在（FAIL 级，不存在则无法部署）、`/mnt`、`/datapool` 是否存在、端口 8000/8009 状态。有 FAIL 先解决再继续。

---

## 第二步：导入镜像

```bash
docker load -i /data/mineru-ppu-fork-3.4.4.tar.gz
docker images mineru:ppu-fork-3.4.4   # 应看到一行镜像信息
```

---

## 第三步：改 compose-ppu.yaml（2 处）

1. **卡号**：`CUDA_VISIBLE_DEVICES: "0"` → 你的空闲卡号（`ppu-smi` 看）
2. **路径**：`/mnt`、`/datapool` → 服务器实际路径（与 V2.txt 一致，默认无需改）

---

## 第四步：启动

```bash
mkdir -p /datapool/mineru_output
docker compose -f /data/compose-ppu.yaml up -d
docker compose -f /data/compose-ppu.yaml logs -f   # 等出现 "Uvicorn running on..."
```

按 `Ctrl+C` 退出日志查看，服务在后台继续运行。

---

## 第五步：验证

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/file_parse \
  -F "files=@/path/to/发票.pdf" \
  -F "backend=vlm-vllm-async-engine" \
  -o /tmp/test.zip
```

---

## 常用运维命令

| 操作     | 命令                                                   |
| -------- | ------------------------------------------------------ |
| 启动服务 | `docker compose -f compose-ppu.yaml up -d`           |
| 查看状态 | `docker compose -f compose-ppu.yaml ps`              |
| 查看日志 | `docker compose -f compose-ppu.yaml logs --tail 100` |
| 重启服务 | `docker compose -f compose-ppu.yaml restart`         |
| 停止服务 | `docker compose -f compose-ppu.yaml down`            |

---

## 遇到问题？

| 现象                     | 可能原因                                                  | 解决                                                                            |
| ------------------------ | --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 卡不可用                 | 设备未透传 / 卡号错                                       | 确认`/dev/alixpu` 存在，`ppu-smi` 看卡号（再填进 `CUDA_VISIBLE_DEVICES`） |
| 健康检查失败             | 模型还在加载                                              | 再等 1-2 分钟                                                                   |
| 端口被占用               | 其他服务用了 8000                                         | 改 compose 里的`--port` / 确认监听端口                                        |
| 解析结果为空             | PDF 损坏或有密码                                          | 换一个正常 PDF 测试                                                             |
| 解析报缺 OriCls 模型     | 镜像构建时下载列表漏了该模型（2026-09-08 前构建的旧镜像） | 见下「镜像缺 OriCls 怎么办」                                                    |
| 收到新代码不知道怎么更新 | 构建在构建机完成                                          | 见上面「更新代码」；构建细节见构建机上的`README-ppu-build.md`                 |

### 镜像缺 OriCls 怎么办（2026-09-08 前构建的旧镜像）

> OriCls（页面方向分类）是旧版 `docker/china/ppu.Dockerfile` 构建时下载列表漏掉的模型，2026-09-08 起已在构建文件补上。若你拿到的是补丁前打的镜像，解析可能报找不到 OriCls / 方向判断异常。

- **有最新镜像** → 找构建人员要新包，按上面「更新代码」重 load 即可。
- **没有最新镜像、又想尽快跑** → 用方式 B（源码挂载 + 模型包挂载）部署，见 `../offline-package-ppu-mount/README-ppu-mount-deploy.md`——该方式通过 compose 挂载补齐 OriCls，无需重打镜像。
