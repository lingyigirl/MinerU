# MinerU PPU 方式 A 缺模型补救——挂载模型包补齐 OriCls

> **面向对象**：已按方式 A（`offline-package-ppu/`）收到 fork 镜像 `mineru:ppu-fork-3.4.4`，
> 但镜像构建早于 2026-09-08，缺少 OriCls（页面方向分类）模型。
> **解决思路**：镜像代码是 3.4.4 正确的，只缺模型——新建「模型包」挂载进去补齐，无需等待重打镜像。
> **预计耗时**：15-20 分钟（模型包合并需联网打包机；部署侧若已收到含 OriCls 的模型包则约 10 分钟）

---

## 什么时候用这份文档

| 你的情况 | 用哪个文档 |
|---|---|
| 方式 A 的 fork 镜像 `mineru:ppu-fork-3.4.4` 已 `docker load`，但解析报**缺 OriCls** / 方向判断异常 | ✅ **用本文档** |
| 基础镜像 `mineru:ppu-vllm-latest` 已重建（2026-09-08 后），OriCls 齐全 | 用 `offline-package-ppu/README-ppu-deploy.md`（方式 A 正常流程） |
| 没有 fork 镜像，但有 `mineru:ppu-vllm-latest` 基础镜像 + 源码包 | 用 `README-ppu-mount-deploy.md`（方式 B 正常流程） |

---

## 补全模型包：合并 OriCls 到原有 mineru_models 并重新压缩

> 本步只需**一次**，在联网打包机上做；把含 OriCls 的新 `mineru-models-3.4.4.tar.gz` 发给部署人员后，部署侧无需再碰打包。

### 背景

之前打包 `mineru-models-3.4.4.tar.gz` 时，`mineru-models-download -s modelscope -m all` 的下载列表**缺 OriCls**（上游 `models_download.py` 的 `model_paths` 未包含该模型），所以旧模型包里没有 `OriCls/` 目录。
2026-09-08 起 `docker/china/ppu.Dockerfile` 已补 `snapshot_download` 补齐该模型，但**旧模型包 / 旧镜像仍然缺**——本文档用挂载方式补救。

### 操作步骤

```bash
cd /data                     # 打包机工作目录

# 1. 解出原有的 mineru_models（如已解压则跳过）
tar xzf mineru-models-3.4.4.tar.gz

# 2. 确认 OriCls 缺失
ls mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/
# 若输出列表中没有 OriCls/，则确认为缺失

# 3. 把 OriCls 合并到原有的 mineru_models
# 方式一（联网直接下到缓存对应位置）：
python3 -c "
from modelscope import snapshot_download
snapshot_download(
    'OpenDataLab/PDF-Extract-Kit-1.0',
    cache_dir='./mineru_models/hub',
    allow_patterns=['models/OriCls/*', 'models/OriCls/*/*'],
)
"
#   该命令会落到 mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/...

# 方式二（从别的机器/已补齐的缓存拷过来，离线场景用）：
# cp -r /path/to/OriCls \
#   mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls

# 4. 确认 OriCls 已合并到位
ls mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/paddle_orientation_classification/
# 应看到 PP-LCNet_x1_0_doc_ori.onnx

# 5. 重新压缩成模型包
tar czf mineru-models-3.4.4.tar.gz mineru_models/
ls -lh mineru-models-3.4.4.tar.gz

# 6. 把新的 mineru-models-3.4.4.tar.gz 随物料发给部署人员
```

> **保持 Modelscope 嵌套结构**：模型包内目录必须是
> ```
> mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/
>   ├── OriCls/   ← 本题补齐
>   ├── Layout/  OCR/  MFR/  TabCls/  TabRec/
> ```
> 与镜像内置 `mineru.json` 的 `models-dir` 指向天然匹配，挂载后无需额外配置。

---

## 你的物料包里有什么

```
mineru:ppu-fork-3.4.4                ← fork 镜像（已 docker load，代码是 3.4.4，缺 OriCls）
mineru-models-3.4.4.tar.gz          ← 模型包（上面的产物，含 OriCls）
compose-ppu.yaml                    ← 方式 A 启动配置（来自 offline-package-ppu/）
check_server_env.sh                 ← 环境检查（用 offline-package-ppu/ 那个，它检查 fork 镜像）
README-ppu-mount-deploy-2.md        ← 本文件
```

> 本包不是标准方式 A 也不是标准方式 B，是「A 的镜像 + B 的模型挂载」的**混合补救**。
> 不含源码包（`mineru-src-3.4.4.tar.gz`）——fork 镜像里代码已是 3.4.4，无需源码挂载。

---

## 第一步：环境检查

在服务器上跑**方式 A 的**检查脚本（它才检查 fork 镜像内的 OriCls）：

```bash
cd /data/offline-package-ppu
bash check_server_env.sh
```

**重点看**：

1. `/dev/alixpu`、`/dev/alixpu_ctl` 是否存在——FAIL 级，缺了无法部署。
2. **第 5b 项预期会出现一条 WARN**：`mineru:ppu-fork-3.4.4 缺 OriCls 模型`。这是**预期**的——正是我们要补救的状态，看到它说明镜像确实缺，继续下一步即可。
3. `/mnt`、`/datapool` 是否存在、端口 8000 是否被占。

> 方式 B 的检查脚本（offline-package-ppu-mount/）只查基础镜像 `mineru:ppu-vllm-latest`，本场景不一定加载了它，不适用。

---

## 第二步：解压模型包

```bash
tar xzf /data/mineru-models-3.4.4.tar.gz -C /data
# 得到 /data/mineru_models/hub/models/OpenDataLab/...（Modelscope hub 结构）

# 确认 OriCls 已包含
find /data/mineru_models -name "PP-LCNet_x1_0_doc_ori.onnx" 2>/dev/null
# 应有输出
```

---

## 第三步：改 compose-ppu.yaml（加模型挂载行）

打开 `compose-ppu.yaml`（方式 A 那份），在 `volumes:` 段**追加一行模型挂载**：

```yaml
      # ---- 模型挂载（补齐 OriCls）----
      # fork 镜像缺 OriCls，挂载模型包补齐（宿主机路径按第二步解压位置改）
      - /data/mineru_models:/root/.cache/modelscope
```

改完的 `volumes:` 段完整样子：

```yaml
    volumes:
      # ---- PPU vLLM 需要 docker.sock（容器内再调度） ----
      - /var/run/docker.sock:/var/run/docker.sock
      # ---- 数据/模型挂载（与 V2.txt 服务器一致，按实际路径改） ----
      - /mnt:/mnt
      - /datapool:/datapool
      # ---- 模型挂载（补齐 OriCls）----
      - /data/mineru_models:/root/.cache/modelscope
```

同时确认：

- `image: mineru:ppu-fork-3.4.4` **保留不动**（代码在镜像里，不用源码挂载）。
- `/mnt`、`/datapool` 改成服务器实际路径。
- `CUDA_VISIBLE_DEVICES` 改成 `ppu-smi` 看到的空闲卡号。

> 不用额外挂 `mineru.json`——镜像内置的已指向 `/root/.cache/modelscope/hub/models/...`，挂载后路径天然匹配。

---

## 第四步：启动 && 验证

```bash
mkdir -p /datapool/mineru_output
docker compose -f /data/compose-ppu.yaml up -d
docker compose -f /data/compose-ppu.yaml logs -f
# 等出现 "Uvicorn running on..."
```

验证：

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/file_parse \
  -F "files=@/path/to/发票.pdf" \
  -F "backend=vlm-vllm-async-engine" \
  -o /tmp/test.zip
```

若仍报缺 OriCls，进容器确认挂载是否生效：

```bash
docker exec -it mineru-ppu \
  ls /root/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/models/OriCls/paddle_orientation_classification/
# 应能看到 PP-LCNet_x1_0_doc_ori.onnx
```

---

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 仍报 OriCls 找不到 | 挂载路径不匹配 / 没生效 | 用上面 `docker exec` 命令看容器内路径；对照宿主机 `/data/mineru_models/hub/...` 结构是否一致，然后 `docker compose restart` |
| 卡不可用 | 设备未透传 / 卡号错 | 确认 `/dev/alixpu` 存在，`ppu-smi` 看卡号，填进 `CUDA_VISIBLE_DEVICES` |
| 健康检查失败 | 模型还在加载 | 再等 1-2 分钟 |
| 端口被占用 | 其他服务用了 8000 | 改 `compose-ppu.yaml` 的 `--port` |
| 解析结果为空 | PDF 损坏或有密码 | 换正常 PDF 测试 |

---

## 后续：拿到新镜像后恢复正常方式 A

下次构建机重建基础镜像（`docker/china/ppu.Dockerfile` 已含 OriCls 补下载）并重打 fork 镜像包后，部署回到正常方式 A：

```bash
docker load -i /data/mineru-ppu-fork-3.4.4.tar.gz
docker compose -f /data/compose-ppu.yaml up -d --force-recreate
```

确认新镜像 OriCls 齐全后，可注释掉/移除第三步加的模型挂载行。

---

> **本操作没有「日常更新代码」步骤**：那是标准方式 B 源码挂载场景的后续动作（换新 `mineru-src-*.tar.gz` 解压覆盖 + restart）。本场景用的是 fork 镜像，代码在镜像内，更新代码需重新构建镜像，与挂载更新不同，因此不适用本次补救流程。
