# MinerU 3.4.4 离线生产部署完整指南

> **适用版本**: MinerU 3.4.4
> **部署方式**: Docker + Docker Compose（离线环境）
> **适用场景**: 无法访问互联网的生产服务器
> **预计耗时**: 30-60 分钟（含镜像构建）
> **前置条件**: 一台有网络的构建机 + 一台生产服务器

---

## 零、部署前必读

### 0.1 文档导航

本指南覆盖从零到一的完整部署流程。如果你已经完成了某个步骤，可直接跳到对应章节：

| 步骤 | 内容 | 在哪里执行 |
|------|------|----------|
| 第一步 | 服务器环境检查 | 生产服务器 |
| 第二步 | 物料准备（构建镜像 + 下载模型） | 构建机（有网络） |
| 第三步 | 传输物料到服务器 | 任意 |
| 第四步 | 生产服务器部署 | 生产服务器 |
| 第五步 | 验证和测试 | 生产服务器 |
| 第六步 | 运维管理 | 生产服务器 |

### 0.2 MinerU 运行原理速览

理解下面这张图，有助于理解部署中每个文件和路径的作用：

```
┌─────────────────────────────────────────────────────────────┐
│                     Docker 容器                              │
│                                                              │
│  mineru-api (Python 3.12.12)                                │
│  ├── /usr/local/lib/python3.12/dist-packages/mineru/        │
│  │   ← pip install 安装的 MinerU 包                          │
│  │                                                           │
│  ├── /root/mineru.json                                      │
│  │   ← 运行时配置（模型路径、S3 等）                          │
│  │   默认路径: ~/mineru.json                                 │
│  │   可通过 MINERU_TOOLS_CONFIG_JSON 覆盖                    │
│  │                                                           │
│  └── /root/.cache/modelscope/hub/models/OpenDataLab/        │
│      ← modelscope 默认缓存目录                               │
│      MinerU 读取时: models-dir 配置 + MINERU_MODEL_SOURCE   │
│                                                              │
│  模型加载逻辑（当 MINERU_MODEL_SOURCE=local）:                │
│    mineru.json → models-dir.pipeline → 加载 pipeline 模型    │
│    mineru.json → models-dir.vlm → 加载 VLM 模型              │
└─────────────────────────────────────────────────────────────┘
```

### 0.3 关键路径说明（是否固定？）

这是新手最容易困惑的问题，先解释清楚：

#### Q: 为什么项目路径是 `/usr/local/lib/python3.12/dist-packages/mineru`？

**不是固定的**。这是由 vllm 基镜像的 Python 安装决定的：

- vllm/vllm-openai:v0.11.2 基于 Ubuntu，内置 Python 3.12
- pip install 安装的包默认放在 `/usr/local/lib/python3.12/dist-packages/`
- 如果基础镜像升级到 Python 3.13，路径会变成 `/usr/local/lib/python3.13/dist-packages/`

> **生产部署不需要关心这个路径！** 只有开发环境（需要源码覆盖挂载）才需要知道。

#### Q: 为什么配置文件是 `/root/mineru.json`？

**不是固定的**。由两个因素决定：

1. 容器以 root 用户运行 → home 目录是 `/root`
2. `config_reader.py` 默认读取 `~/mineru.json`（即 `/root/mineru.json`）

可以通过环境变量覆盖：

```bash
export MINERU_TOOLS_CONFIG_JSON=/custom/path/my-config.json
```

#### Q: 为什么模型放在 `/root/.cache/modelscope/hub/models/OpenDataLab/`？

**不是固定的**。取决于：

- **下载工具**：`mineru-models-download -s modelscope` 默认下载到 modelscope 缓存目录
- **缓存目录**：可通过 `MODELSCOPE_CACHE` 环境变量修改
- **运行时加载**：MinerU 根据 `mineru.json` 中的 `models-dir.pipeline` 和 `models-dir.vlm` 配置查找模型，**与缓存目录无关**

> **结论**：生产部署时，模型可以放在任意位置，只要 `mineru.json` 的 `models-dir` 指向正确路径即可。

### 0.4 本指南使用的路径约定

| 用途 | 宿主机路径 | 容器内路径 | 是否必须 |
|------|-----------|-----------|---------|
| MinerU 配置 | `/root/mineru_8011.json` | `/root/mineru.json` | 可自定义 |
| Pipeline 模型 | `/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0` | `/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0` | 可自定义，与 mineru.json 一致即可 |
| VLM 模型 | `/data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B` | 同左 | 可自定义，与 mineru.json 一致即可 |
| 输出目录 | `/data/mineru_output_8011` | `/vllm-workspace/output` | 可自定义 |

---

## 一、服务器环境检查

### 1.1 运行自动检查脚本

```bash
# 在生产服务器上执行
bash deploy/check_server_env.sh
```

脚本会自动检测：
- CPU（核心数、架构、AVX2 指令集）
- GPU（每张卡的型号、显存、计算能力、已用量）
- 内存（总量、可用量）
- 磁盘（分区容量、所在分区剩余空间）
- CUDA/驱动版本
- Docker 环境（版本、daemon 状态、GPU 运行时）
- 端口占用情况
- 网络连通性

### 1.2 硬件最低要求速查

| 后端 | 最低显存 | 最低内存 | 推荐配置 |
|------|---------|---------|---------|
| `hybrid-auto-engine`（默认） | 10GB | 16GB | 32GB+ 内存 + RTX 3090+ |
| `pipeline` | 6GB | 16GB | 32GB+ 内存 + RTX 2060+ |
| `vlm-auto-engine` | 8GB | 16GB | 32GB+ 内存 + RTX 3090+ |

**本指南默认使用 `hybrid-auto-engine`**（精度最高，需要 GPU）。

### 1.3 软件要求

| 软件 | 最低版本 | 本指南版本 |
|------|---------|----------|
| Docker | 20.10+ | 28.1.1 |
| Docker Compose | v2.0+ | v2.35.1 |
| NVIDIA 驱动 | 支持 CUDA 12.0+ | 驱动 580.105.08 (CUDA 13.0) |
| NVIDIA Container Toolkit | 1.13+ | 运行 `nvidia-smi` 验证 |
| 操作系统 | Linux 2019+（Ubuntu 20.04+ / CentOS 8+） | 任意 |

---

## 二、物料准备（在构建机上执行）

> **前提**：构建机需要能访问互联网（PyPI + ModelScope/HuggingFace）。
> 构建机不需要 GPU。

### 2.1 获取源码

```bash
# 方式 1：从 Git 仓库克隆（推荐）
git clone <your-repo-url> MinerU
cd MinerU
git checkout release3.4.4

# 方式 2：如果已经克隆了仓库
cd /path/to/MinerU
git checkout release3.4.4
git pull origin release3.4.4
```

### 2.2 确认版本号

```bash
cat mineru/version.py
# 应输出: __version__ = "3.4.4"
```

### 2.3 构建 Docker 镜像

```bash
cd /path/to/MinerU
IMAGE_NAME=mineru:3.4.4 bash build-docker.sh
```

**构建过程说明**：
- 基础镜像：`vllm/vllm-openai:v0.11.2`（约 8GB，首次需下载）
- 安装 `mineru[core]` + `mineru-vl-utils>=1.0.0` + `pdftext<0.7.0`
- 构建时间：约 10-15 分钟（取决于网络和 CPU）
- 最终镜像大小：约 30GB

**常见构建问题**：

| 错误 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: No module named 'mineru'` | Dockerfile COPY 顺序错误 | 确保 `COPY mineru/` 在 `pip install` 之前 |
| `unexpected keyword argument 'enable_table_formula_eq_wrap'` | mineru-vl-utils 版本过低 | 检查 >=1.0.0 |
| 网络超时 | pip 下载慢 | 使用国内镜像：`pip install -i https://mirrors.aliyun.com/pypi/simple` |

### 2.4 下载模型文件

```bash
# 使用 ModelScope 下载（国内更快）
export MINERU_MODEL_SOURCE=modelscope
mineru-models-download -s modelscope -m all

# 或使用 HuggingFace 下载
# export MINERU_MODEL_SOURCE=huggingface
# mineru-models-download -s huggingface -m all
```

下载后确认模型文件：

```bash
# pipeline 模型（约 2.5GB）
ls ~/.cache/modelscope/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/
# 应包含: models/, config.json 等

# VLM 模型（约 2.5GB）
ls ~/.cache/modelscope/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B/
# 应包含: model files, config.json, tokenizer 等
```

### 2.5 导出镜像和打包模型

```bash
# 导出 Docker 镜像
docker save mineru:3.4.4 | gzip > mineru-3.4.4.tar.gz

# 打包模型文件
tar czf mineru-models-3.4.4.tar.gz -C ~/.cache/modelscope .

# 准备配置文件模板（稍后在生产服务器上修改）
cp mineru.template.json mineru-prod-template.json

# 确认文件大小
ls -lh mineru-3.4.4.tar.gz mineru-models-3.4.4.tar.gz
# 镜像约 8-10GB (压缩后)
# 模型约 4-5GB (压缩后)
```

---

## 三、传输物料到生产服务器

```bash
# 在生产服务器上创建部署目录
ssh root@production-server "mkdir -p /data/mineru-deploy"

# 传输镜像
scp mineru-3.4.4.tar.gz root@production-server:/data/mineru-deploy/

# 传输模型
scp mineru-models-3.4.4.tar.gz root@production-server:/data/mineru-deploy/

# 传输配置文件模板
scp mineru-prod-template.json root@production-server:/data/mineru-deploy/

# 传输部署脚本
scp deploy/check_server_env.sh root@production-server:/data/mineru-deploy/
scp deploy/compose-prod.yaml root@production-server:/data/mineru-deploy/
```

---

## 四、生产服务器部署

> **以下所有命令在生产服务器上执行。**

### 4.1 环境检查

```bash
cd /data/mineru-deploy
bash check_server_env.sh
```

确认检查通过后继续。

### 4.2 导入 Docker 镜像

```bash
cd /data/mineru-deploy

# 导入镜像
gunzip -c mineru-3.4.4.tar.gz | docker load

# 验证导入成功
docker images mineru:3.4.4
# 应显示: mineru  3.4.4  <IMAGE_ID>  ...  约 30GB
```

### 4.3 解压模型文件

```bash
# 创建模型目录
mkdir -p /data/mineru_models

# 解压模型
tar xzf mineru-models-3.4.4.tar.gz -C /data/mineru_models

# 验证模型文件
ls /data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0/
ls /data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B/
```

### 4.4 配置 mineru.json

```bash
# 创建配置文件
cat > /root/mineru-8011.json << 'EOF'
{
    "bucket_info": {
        "bucket-name-1": ["ak", "sk", "endpoint"]
    },
    "latex-delimiter-config": {
        "display": { "left": "$$", "right": "$$" },
        "inline": { "left": "$", "right": "$" }
    },
    "llm-aided-config": {
        "title_aided": {
            "api_key": "your_api_key",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-plus",
            "enable_thinking": false,
            "enable": false
        }
    },
    "models-dir": {
        "pipeline": "/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",
        "vlm": "/data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"
    },
    "config_version": "1.3.2"
}
EOF
```

> **关键配置项说明**：
> - `models-dir.pipeline`：Pipeline 模型的实际解压路径，**必须与实际路径一致**
> - `models-dir.vlm`：VLM 模型的实际解压路径，**必须与实际路径一致**
> - `llm-aided-config`：LLM 辅助标题分类（可选，默认关闭）
> - `bucket_info`：S3 存储桶配置（可选）

### 4.5 创建 Docker Compose 配置

```bash
cat > /data/mineru-deploy/compose-8011.yaml << 'EOF'
# ============================================================
# MinerU 3.4.4 生产环境 Docker Compose 配置
# 服务端口: 8011
# 使用 GPU: 按需修改 device_ids
# ============================================================
services:
  mineru-api:
    image: mineru:3.4.4
    container_name: mineru-api-8011
    restart: always
    ports:
      - "8011:8000"
    environment:
      # ---- 模型来源 ----
      MINERU_MODEL_SOURCE: local          # 离线部署必须设为 local
      # ---- 计算设备 ----
      MINERU_DEVICE_MODE: cuda
      # ---- 解析后端（默认 hybrid-auto-engine，精度最高） ----
      # 可选: pipeline | vlm-auto-engine | hybrid-auto-engine
      MINERU_BACKEND: hybrid-auto-engine
      # ---- 功能开关 ----
      MINERU_VLM_FORMULA_ENABLE: "true"
      MINERU_VLM_TABLE_ENABLE: "true"
      # ---- 跨页表格合并 ----
      MINERU_TABLE_MERGE_ENABLE: "true"
      # ---- 滑窗处理页数（控制显存峰值） ----
      MINERU_PROCESSING_WINDOW_SIZE: "64"
      # ---- 日志 ----
      MINERU_LOG_LEVEL: INFO
      MINERU_LOG_FILE_ENABLE: "false"     # Docker 部署建议关闭文件日志
      # ---- API 输出目录 ----
      MINERU_API_OUTPUT_ROOT: /vllm-workspace/output
      # ---- 配置文件路径覆盖（可选） ----
      # MINERU_TOOLS_CONFIG_JSON: /root/mineru.json
    entrypoint: mineru-api
    volumes:
      # ---- 配置文件（只读挂载） ----
      - /root/mineru-8011.json:/root/mineru.json:ro
      # ---- 模型文件（只读挂载，多容器可共享） ----
      - /data/mineru_models:/data/mineru_models:ro
      # ---- 输出目录 ----
      - /data/mineru_output_8011:/vllm-workspace/output
    command:
      --host 0.0.0.0
      --port 8000
      --gpu-memory-utilization 0.5     # KV 缓存占比，显存不足时降低 (如 0.4)
      # --data-parallel-size 2         # 多 GPU 并行（单 GPU 注释掉）
      # --allow-public-http-client     # 如客户端也在公网则启用
    ulimits:
      memlock: -1                       # vllm 需要锁定内存
      stack: 67108864                   # 栈大小 64MB
    ipc: host                           # vllm 多进程通信需要
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval: 30s
      timeout: 10s
      start-period: 180s                # 启动宽限期（模型加载需 60-120 秒）
      retries: 3
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["0"]         # ← 按实际 GPU 修改
              capabilities: [gpu]
EOF
```

### 4.6 创建输出目录

```bash
mkdir -p /data/mineru_output_8011
```

### 4.7 启动服务

```bash
cd /data/mineru-deploy

# 启动
docker compose -f compose-8011.yaml up -d

# 查看启动日志（等待模型加载）
docker compose -f compose-8011.yaml logs -f
```

**启动过程关键日志解读**：

```
# 1. vllm 引擎初始化
INFO: Started server process [1]
INFO: Waiting for application startup.

# 2. VLM 模型加载（约 60-120 秒）
INFO: init engine (profile, create, load) took XX seconds

# 3. Pipeline 模型加载
INFO: Layout Predict ...
INFO: OCR-det ...
INFO: MFR ...

# 4. 服务就绪
INFO: Uvicorn running on http://0.0.0.0:8000
```

### 4.8 健康检查

```bash
# 基本健康检查
curl http://localhost:8011/health
# 期望输出: {"status":"ok"}

# 查看 API 文档
# 浏览器访问: http://<服务器IP>:8011/docs
```

---

## 五、验证和测试

### 5.1 基本功能测试

```bash
# 准备测试 PDF（项目中有自带）
# /path/to/MinerU/tests/unittest/pdfs/test.pdf

# 通过 API 解析
curl -X POST http://localhost:8011/file_parse \
  -F "file=@/path/to/test.pdf" \
  -F "backend=hybrid-auto-engine" \
  -o /tmp/test_output.zip

# 解压查看结果
unzip -o /tmp/test_output.zip -d /tmp/test_output/
ls /tmp/test_output/
# 应包含: *.md, images/, content_list.json 等
```

### 5.2 异步任务测试

```bash
# 提交异步任务
TASK_RESPONSE=$(curl -s -X POST http://localhost:8011/tasks \
  -F "file=@/path/to/test.pdf")
TASK_ID=$(echo "$TASK_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "Task ID: $TASK_ID"

# 轮询状态
curl -s "http://localhost:8011/tasks/$TASK_ID"

# 下载结果
curl -s "http://localhost:8011/tasks/$TASK_ID/result" -o /tmp/task_output.zip
```

### 5.3 性能基准测试

```bash
# 测试解析速度（time 命令）
time curl -X POST http://localhost:8011/file_parse \
  -F "file=@/path/to/your-real-document.pdf" \
  -F "backend=hybrid-auto-engine" \
  -o /tmp/bench_output.zip

# 记录解析耗时，作为后续性能对比基准
```

### 5.4 验证清单

- [ ] `/health` 返回 200
- [ ] API 文档页面 `/docs` 可访问
- [ ] PDF 解析正常完成（`/file_parse`）
- [ ] 异步任务正常（`/tasks` 提交 + 轮询 + 下载）
- [ ] 输出 Markdown 内容完整
- [ ] 表格解析正确（对比原 PDF 表格）
- [ ] 公式解析正确（如文档含公式）
- [ ] 日志中无 ERROR 级别异常

---

## 六、多 GPU 多服务部署（充分利用硬件）

如果你的服务器有多张 GPU（本指南示例环境有 4× RTX 4090），可以部署多个 mineru-api 实例分别绑定不同 GPU：

### 6.1 方案一：多实例 + 不同端口

```yaml
# compose-multi-gpu.yaml
services:
  mineru-api-gpu0:
    image: mineru:3.4.4
    container_name: mineru-api-gpu0
    restart: always
    ports:
      - "8010:8000"
    environment:
      MINERU_MODEL_SOURCE: local
      MINERU_DEVICE_MODE: cuda
      MINERU_BACKEND: hybrid-auto-engine
      MINERU_LOG_FILE_ENABLE: "false"
    entrypoint: mineru-api
    volumes:
      - /root/mineru-8011.json:/root/mineru.json:ro
      - /data/mineru_models:/data/mineru_models:ro
      - /data/mineru_output_8010:/vllm-workspace/output
    command:
      --host 0.0.0.0 --port 8000
      --gpu-memory-utilization 0.5
    ulimits:
      memlock: -1
      stack: 67108864
    ipc: host
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["0"]
              capabilities: [gpu]

  mineru-api-gpu1:
    image: mineru:3.4.4
    container_name: mineru-api-gpu1
    restart: always
    ports:
      - "8011:8000"
    environment:
      MINERU_MODEL_SOURCE: local
      MINERU_DEVICE_MODE: cuda
      MINERU_BACKEND: hybrid-auto-engine
      MINERU_LOG_FILE_ENABLE: "false"
    entrypoint: mineru-api
    volumes:
      - /root/mineru-8011.json:/root/mineru.json:ro
      - /data/mineru_models:/data/mineru_models:ro
      - /data/mineru_output_8011:/vllm-workspace/output
    command:
      --host 0.0.0.0 --port 8000
      --gpu-memory-utilization 0.5
    ulimits:
      memlock: -1
      stack: 67108864
    ipc: host
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["1"]
              capabilities: [gpu]
```

### 6.2 方案二：Router 负载均衡

```bash
# 启动 Router（自动分配请求到最空闲的 worker）
mineru-router --host 0.0.0.0 --port 8002 \
  --upstream-url http://localhost:8010 \
  --upstream-url http://localhost:8011

# 客户端请求统一访问 Router 端口
curl -X POST http://localhost:8002/file_parse \
  -F "file=@document.pdf"
```

### 6.3 C/S 分离部署方案

将 VLM 推理和业务逻辑分离到不同节点：

```
┌─────────────────────┐     ┌──────────────────────────┐
│  GPU 节点 (30000)    │     │  业务节点 (8000)          │
│                      │     │                           │
│  mineru-openai-      │◄───│  mineru-api               │
│  server              │     │  -b vlm-http-client      │
│  (纯 VLM 推理)       │     │  -u http://gpu:30000     │
│                      │     │  (不加载 VLM 模型)        │
└─────────────────────┘     └──────────────────────────┘
```

```bash
# GPU 节点
docker run -d --gpus all --name vlm-server \
  -p 30000:30000 \
  -v /data/mineru_models:/data/mineru_models:ro \
  mineru:3.4.4 \
  mineru-openai-server --host 0.0.0.0 --port 30000

# 业务节点（可以是 CPU-only 或低显存机器）
docker run -d --name mineru-api \
  -p 8000:8000 \
  mineru:3.4.4 \
  mineru-api --host 0.0.0.0 --port 8000 \
  --backend vlm-http-client \
  --api-url http://gpu-node:30000
```

---

## 七、常见部署方案选择指南

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| 单 GPU 开发/测试 | 单实例 mineru-api | 最简单 |
| 单 GPU 生产 | 单实例 mineru-api + 调优 | 足够应对中低负载 |
| 多 GPU 高吞吐 | 多实例（每 GPU 一个）+ Router | 水平扩展 |
| GPU + 多 CPU 节点 | C/S 分离：GPU 推理 + CPU 业务 | 资源解耦，灵活扩缩容 |
| 纯 CPU 环境 | mineru-api -b pipeline | 无 GPU 可用时 |
| KVP 票据/表单 | mineru-api + KVP 引擎 | custom 模块 |

---

## 八、运维管理

### 8.1 日常操作

```bash
# 查看服务状态
docker compose -f compose-8011.yaml ps

# 查看实时日志
docker compose -f compose-8011.yaml logs -f

# 查看最近 100 行日志
docker compose -f compose-8011.yaml logs --tail 100

# 重启服务（代码未变，配置变更后）
docker compose -f compose-8011.yaml restart

# 停止服务
docker compose -f compose-8011.yaml down

# 进入容器调试
docker exec -it mineru-api-8011 bash
```

### 8.2 性能调优

| 参数 | 默认值 | 调整建议 |
|------|--------|---------|
| `--gpu-memory-utilization` | 0.9 (vllm) | 单 GPU 跑多服务时降低到 0.4-0.5 |
| `MINERU_PROCESSING_WINDOW_SIZE` | 64 | 内存充裕可上调到 128；显存紧张下调到 32 |
| `MINERU_API_MAX_CONCURRENT_REQUESTS` | 3 | 高并发场景上调，注意显存消耗 |
| `--data-parallel-size` | 1 | 多 GPU 时增加并行度 |

### 8.3 日志管理

```bash
# 容器日志回滚（防止占满磁盘）
# 在 compose.yaml 中添加 logging 配置：
#
# logging:
#   driver: "json-file"
#   options:
#     max-size: "100m"
#     max-file: "3"

# 手动清理日志
docker compose -f compose-8011.yaml down
docker system prune -f
docker compose -f compose-8011.yaml up -d
```

### 8.4 升级到新版本

```bash
# 1. 在构建机上准备新版物料
# 2. 传输到生产服务器
# 3. 停止旧服务
docker compose -f compose-8011.yaml down

# 4. 导入新镜像
docker load < mineru-NEW_VERSION.tar.gz

# 5. 更新 compose.yaml 中的 image tag
# 6. 启动新服务
docker compose -f compose-8011.yaml up -d

# 7. 验证
curl http://localhost:8011/health

# 8. 如有问题，回滚
# 改回旧 image tag，重新 up
```

### 8.5 备份策略

```bash
# 关键文件备份
tar czf mineru-backup-$(date +%Y%m%d).tar.gz \
  /root/mineru-8011.json \
  /data/mineru-deploy/compose-8011.yaml \
  /data/mineru_output_8011/

# 镜像备份（已有 tar.gz 文件，无需重复导出）
```

### 8.6 监控告警建议

```bash
# 1. 健康检查 Cron（每 5 分钟）
*/5 * * * * curl -f http://localhost:8011/health || \
  echo "MinerU health check failed at $(date)" | tee -a /var/log/mineru-monitor.log

# 2. GPU 显存监控
*/10 * * * * nvidia-smi --query-gpu=memory.used --format=csv,noheader >> /var/log/gpu-monitor.log

# 3. 磁盘空间监控
0 * * * * df -h /data/mineru_output_8011 | tail -1 >> /var/log/disk-monitor.log
```

---

## 九、常见问题排查

### 9.1 启动失败

| 现象 | 原因 | 解决 |
|------|------|------|
| `Error: No such image` | 镜像未导入或 tag 不匹配 | `docker images` 检查，确认 image tag |
| `could not select device driver "nvidia"` | NVIDIA Container Toolkit 未安装 | `apt install nvidia-container-toolkit` |
| `ValueError: Free memory on device is less than desired` | GPU 显存不足 | 降低 `--gpu-memory-utilization` 或清理占用进程 |
| `Permission denied` | 文件权限不足 | `chmod 644 /root/mineru-8011.json` |
| 端口被占用 | 其他进程占用 8011 | `lsof -i :8011`，换端口或停掉占用进程 |

### 9.2 模型加载失败

| 现象 | 原因 | 解决 |
|------|------|------|
| `HFValidationError` | MINERU_MODEL_SOURCE=local 但模型不存在 | 检查 mineru.json 路径与实际解压位置一致 |
| `Can't load the configuration of...` | VLM 模型版本不匹配 | 确认模型目录完整，版本与 mineru 匹配 |
| `FileNotFoundError: ...paddleocr_torch/xxx.pth` | Pipeline 模型未挂载 | 检查 models-dir.pipeline 路径和挂载 |

### 9.3 解析结果异常

| 现象 | 原因 | 解决 |
|------|------|------|
| 解析结果为空 | PDF 损坏或加密 | 尝试其他 PDF 测试 |
| 表格解析不正确 | 复杂表格超出能力 | 尝试其他后端或降级到 VLM 模式 |
| 中文识别错误 | OCR 语言设置 | 添加 `-l zh` 参数或 `lang: zh` |
| 输出文件过大 | 图片分辨率高 | 调整 DPI 设置 |

### 9.4 性能问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 解析速度慢 | 正常现象（VLM 推理每次都需要时间） | 调整 processing_window_size |
| 内存持续增长 | 内存泄漏或并发过高 | 降低并发，添加定期重启 |
| 显存持续占满 | gpu-memory-utilization 过高 | 降低到 0.3-0.4 |

---

## 十、附录

### 10.1 完整环境变量速查

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MINERU_MODEL_SOURCE` | `huggingface` | 模型来源: local/modelscope/huggingface |
| `MINERU_DEVICE_MODE` | 自动检测 | 计算设备: cuda/cpu/npu/mps |
| `MINERU_BACKEND` | `hybrid-auto-engine` | 后端: pipeline/vlm-auto-engine/hybrid-auto-engine |
| `MINERU_TOOLS_CONFIG_JSON` | `mineru.json` | 配置文件路径 |
| `MINERU_VLM_FORMULA_ENABLE` | `true` | 公式识别开关 |
| `MINERU_VLM_TABLE_ENABLE` | `true` | 表格识别开关 |
| `MINERU_TABLE_MERGE_ENABLE` | `true` | 跨页表格合并 |
| `MINERU_PROCESSING_WINDOW_SIZE` | `64` | 滑窗处理页数 |
| `MINERU_API_MAX_CONCURRENT_REQUESTS` | `3` | 最大并发请求 |
| `MINERU_API_OUTPUT_ROOT` | 临时目录 | 输出根目录 |
| `MINERU_LOG_LEVEL` | `INFO` | 日志级别 |
| `MINERU_LOG_FILE_ENABLE` | `true` | 文件日志开关 |
| `MINERU_PDF_RENDER_TIMEOUT` | `300` | PDF 渲染超时（秒） |

### 10.2 mineru-api 命令行参数速查

| 参数 | 说明 | 示例 |
|------|------|------|
| `--host` | 监听地址 | `--host 0.0.0.0` |
| `--port` | 监听端口 | `--port 8000` |
| `--backend` | 后端引擎 | `--backend hybrid-auto-engine` |
| `--gpu-memory-utilization` | vllm KV 缓存占比 | `--gpu-memory-utilization 0.5` |
| `--data-parallel-size` | 多 GPU 并行数 | `--data-parallel-size 2` |
| `--api-url` | VLM HTTP 客户端 URL | `--api-url http://remote:30000` |
| `--lang` | OCR 语言提示 | `--lang zh` |

### 10.3 mineru.json 完整配置参考

```json
{
    "bucket_info": {
        "bucket-name-1": ["ak", "sk", "endpoint"]
    },
    "latex-delimiter-config": {
        "display": { "left": "$$", "right": "$$" },
        "inline": { "left": "$", "right": "$" }
    },
    "llm-aided-config": {
        "title_aided": {
            "api_key": "your_api_key",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-plus",
            "enable_thinking": false,
            "enable": false
        }
    },
    "models-dir": {
        "pipeline": "/data/mineru_models/hub/models/OpenDataLab/PDF-Extract-Kit-1___0",
        "vlm": "/data/mineru_models/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B"
    },
    "config_version": "1.3.2"
}
```

### 10.4 相关文档链接

| 文档 | 路径 |
|------|------|
| 升级指南 | `agents_logs/edits/2026-08-03_mineru-3.2.0-to-3.4.4-upgrade-guide.md` |
| Docker 升级实战 | `docs/docker-image-upgrade-guide.md` |
| 架构说明 | `docs/zh/dev/架构说明.md` |
| API 接口说明 | `docs/zh/dev/接口说明.md` |
| 服务器检查脚本 | `deploy/check_server_env.sh` |
