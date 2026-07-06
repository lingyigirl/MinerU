# MinerU Docker 镜像构建与升级指南

基于 `mineru:2.7.6` → `mineru:4.2.0` 升级实战经验总结，涵盖所有已知坑点和完整操作步骤。

## 前置准备

### 检查清单

在开始构建之前，确认以下事项：

- [ ] `develop` 分支源码已稳定，所有自定义代码已测试通过
- [ ] `Dockerfile` 中的依赖版本约束与 `pyproject.toml` 一致
- [ ] 宿主机有足够磁盘空间（镜像约 30GB）
- [ ] GPU 显存足够（47GB 卡建议 `gpu_memory_utilization <= 0.55`）
- [ ] 旧容器不需要停机（不同镜像 tag 互不影响）

### 关键文件一览

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 镜像构建定义 |
| `build-docker.sh` | 构建脚本入口 |
| `mineru/version.py` | `__version__` 值，被 `pyproject.toml` 动态读取 |
| `pyproject.toml` | 依赖约束（`pdftext`、`mineru-vl-utils` 等版本在此控制） |
| `compose.yaml` | 容器运行配置（镜像 tag、端口、GPU、挂载） |
| `mineru.json` | 模型路径配置（被挂载进容器 `/root/mineru.json`） |

---

## 操作步骤

### 步骤 1：更新版本号

```bash
# 编辑 mineru/version.py
# __version__ = "2.7.6"  →  __version__ = "5.0.0"
```

### 步骤 2：检查并更新 Dockerfile

#### 2.1 COPY 顺序（重要！）

**源码必须在 `pip install` 之前 COPY，否则构建失败：**

```dockerfile
# ✅ 正确顺序
COPY pyproject.toml README.md ./
COPY mineru/ ./mineru/          # 必须在前
RUN python3 -m pip install -e ".[core]" ...  # 需要 version.py

# ❌ 错误顺序（会导致 ModuleNotFoundError: No module named 'mineru'）
COPY pyproject.toml README.md ./
RUN python3 -m pip install -e ".[core]" ...
COPY mineru/ ./mineru/
```

> **原因：** `pyproject.toml` 通过 `[tool.setuptools.dynamic] version = { attr = "mineru.version.__version__" }` 动态读取版本号，`pip install` 时必须有 `mineru/version.py` 存在。

#### 2.2 ENTRYPOINT 约束

```dockerfile
# ✅ 正确：只写入口命令，参数由 compose.yaml 传入
ENTRYPOINT ["mineru-api"]

# ❌ 错误：会导致 compose.yaml 的 command 无法追加
ENTRYPOINT ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]
```

#### 2.3 关键依赖版本约束

这三行依赖需要在 `pyproject.toml` 和 `Dockerfile` 中保持一致：

```dockerfile
RUN python3 -m pip install --no-cache-dir -e ".[core]" --break-system-packages && \
    python3 -m pip install --no-cache-dir "mineru-vl-utils>=1.0.0" --break-system-packages && \
    python3 -m pip install --no-cache-dir "pdftext<0.7.0" --break-system-packages && \
    python3 -m pip cache purge
```

| 依赖 | 约束 | 问题现象 |
|---|---|---|
| `mineru-vl-utils` | `>=1.0.0` | 过低会报 `unexpected keyword argument 'enable_table_formula_eq_wrap'` |
| `pdftext` | `<0.7.0` | `0.7.0` 的 `get_chars()` 返回 `PageChars` 对象不可迭代，导致 `TypeError: 'PageChars' object is not iterable` |

> **升级前务必检查：** `pip index versions pdftext` 看最新版是否引入了破坏性变更，必要时先在新容器中验证再构建。

#### 2.4 基础镜像

当前使用 `vllm/vllm-openai:v0.11.2`。如果上游要求更新的 vllm 版本，修改 `FROM` 行即可。

### 步骤 3：更新 compose.yaml

```yaml
services:
  mineru-zhangbo:
    image: mineru:5.0.0   # ← 改这里
    # ... 其余不变
```

**无需修改的部分：**
- `volumes` — 模型持久化挂载（`/zhangbo/mineru_models:/root/.cache/modelscope`）保持不变
- `environment` — `MINERU_MODEL_SOURCE: local` 保持不变（模型已在持久化目录中）
- `command` — GPU 参数不变

### 步骤 4：构建镜像

```bash
cd /zhangbo/Mineru
IMAGE_NAME=mineru:5.0.0 bash build-docker.sh
```

> 构建时间约 10-15 分钟（含依赖安装），镜像大小约 30GB。

### 步骤 5：验证新镜像

**5.1 先在不对旧服务产生影响的前提下启动测试：**

```bash
# 停掉本容器（不影响其他使用旧镜像的容器）
docker compose down

# 启动新容器
docker compose up -d

# 查看启动日志，确认无异常
docker compose logs -f
```

**5.2 发送测试请求验证：**

```bash
curl -X POST http://localhost:8011/file_parse \
  -F "file=@/path/to/test.pdf" \
  -F "backend=hybrid-auto-engine"
```

**5.3 快速回归检查清单：**

- [ ] 容器正常启动，health check 返回 200
- [ ] VLM 模型加载成功（日志出现 `init engine ... took XX seconds`）
- [ ] Pipeline 模型加载成功（日志出现 `Layout Predict`、`OCR-det`）
- [ ] PDF 解析正常完成，无异常堆栈
- [ ] 输出目录生成正确的 Markdown 文件

### 步骤 6：回滚（如有问题）

```bash
# 改回旧镜像版本
# compose.yaml: image: mineru:5.0.0 → image: mineru:4.2.0
docker compose down && docker compose up -d
```

---

## 常见错误速查表

| 错误信息 | 根因 | 修复 |
|---|---|---|
| `ModuleNotFoundError: No module named 'mineru'` | Dockerfile COPY 顺序错误 | 源码 COPY 放在 pip install 之前 |
| `unexpected keyword argument 'enable_table_formula_eq_wrap'` | `mineru-vl-utils` 版本过低 | 升级 `mineru-vl-utils>=1.0.0` |
| `TypeError: 'PageChars' object is not iterable` | `pdftext>=0.7.0` 破坏性变更 | Pin `pdftext<0.7.0` |
| `HFValidationError: Repo id must be...` | `MINERU_MODEL_SOURCE=local` 但模型目录不存在 | 先 `modelscope` 下载模型再切回 `local` |
| `ValueError: Free memory on device ... is less than desired` | GPU 显存被其他进程占用 | 降低 `gpu_memory_utilization` 或停掉占用进程 |
| `FileNotFoundError: ...paddleocr_torch/xxx.pth` | Pipeline 模型未挂载/未下载 | 确保 `PDF-Extract-Kit-1.0` 在持久化目录或挂载路径中 |
| `Can't load the configuration of...` | VLM 模型版本不匹配（如 2509 vs 2605） | 确认 `mineru.json` 中路径与实际模型目录一致 |

---

## 两个重要原则

### 1. 新旧镜像完全隔离，可并行运行

```
mineru-api2  →  image: mineru:2.7.6  →  端口 80000, GPU 0
mineru-zhangbo → image: mineru:5.0.0  →  端口 8011, GPU 2
```

不同 tag 的镜像是独立实体，构建新镜像不会影响已运行的老容器。

### 2. 模型缓存通过宿主机目录持久化

```yaml
volumes:
  - /zhangbo/mineru_models:/root/.cache/modelscope
```

两个好处：
- 容器重建不丢失模型（避免每次重建后重新下载 2GB+ 的模型文件）
- 多个容器可共享同一份模型缓存（节省磁盘空间）

---

## 依赖版本兼容性矩阵

构建前参照此表确认 `pyproject.toml` 和 Dockerfile 中的版本约束：

| 版本组件 | 约束来源 | 当前值 | 检查方式 |
|---|---|---|---|
| 基础镜像 | `FROM` | `vllm/vllm-openai:v0.11.2` | 查看上游 Release Note |
| `mineru-vl-utils` | Dockerfile 显式 install | `>=1.0.0` | 对比 `vlm_analyze.py` 传入参数与包版本 |
| `pdftext` | Dockerfile 显式 install | `<0.7.0`（Pin） | `pip index versions pdftext` 检查最新版 |
| `torch` | `mineru[pipeline]` | `>=2.6.0,<3` | 确认与 vllm 基镜像兼容 |
| `vllm` | 基镜像自带 | `0.11.2` | `docker run --rm mineru:5.0.0 pip show vllm` |
| `transformers` | `mineru[vlm]` | `>=4.57.3,<5.0.0` | 确认与 vllm 版本兼容 |
