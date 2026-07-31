# CLAUDE.md

本文件为 Claude Code（claude.ai/code）在此仓库中工作时提供指导。

> **重要**：编码规范和行为准则详见 [.claudecode-rules.md](.claudecode-rules.md)。
> 每次新会话必须先阅读该文件，了解语言规范、代码风格、异常处理、日志规范等强制性规则。
> 本文件侧重项目架构、常用命令和自定义代码约定。

## 常用命令

### 安装
```bash
pip install -e ".[core]"    # 核心：vlm + pipeline + gradio
pip install -e ".[all]"     # 全部后端（含平台相关：vllm/linux, lmdeploy/windows, mlx/macOS）
pip install -e ".[test]"    # 测试依赖
pip install -e ".[vlm]"     # 仅 VLM：torch + transformers + accelerate
pip install -e ".[pipeline]"# 仅 pipeline：torch + torchvision + onnxruntime + OCR 依赖
pip install -e ".[vllm]"    # vLLM 推理服务（Linux）
pip install -e ".[lmdeploy]"# LMDeploy 推理服务（Windows）
pip install -e ".[mlx]"     # MLX-VLM（Apple Silicon / macOS）
pip install -e ".[gradio]"  # 仅 Gradio Web UI
```

### 运行 MinerU
```bash
# CLI（未指定 --api-url 时自动启动本地 API 子进程）
mineru -p input.pdf -o output_dir                    # -p/--path（必填），-o/--output（必填）
mineru -p input.pdf -o output_dir -b pipeline        # 指定后端
mineru -p input.docx -o output_dir                   # 原生 DOCX/PPTX/XLSX 解析
mineru -p input.pdf -o output_dir -m ocr             # OCR 模式（auto/txt/ocr）
mineru -p input.pdf -o output_dir -l zh              # 语言提示（ja, zh, en, ko, ...）
mineru -p input.pdf -o output_dir -s 1 -e 5          # 页面范围（起始/结束）
mineru -p input.pdf -o output_dir -f False           # 禁用公式识别
mineru -p input.pdf -o output_dir -t False           # 禁用表格识别
mineru -p input.pdf -o output_dir --image-analysis   # 启用 VLM 图片分析

# 连接远程 API（跳过本地子进程启动）
mineru -p input.pdf -o output_dir --api-url http://remote:8000

# API 服务（开发模式，自动重载）
mineru-api --host 0.0.0.0 --port 8000 --backend hybrid-engine --reload

# API 服务（启动时预加载 VLM 模型，加快首次请求）
mineru-api --host 0.0.0.0 --port 8000 --enable-vlm-preload true

# API 服务（绑定 0.0.0.0 时允许公网 HTTP 客户端后端）
mineru-api --host 0.0.0.0 --port 8000 --allow-public-http-client

# 多 GPU 路由
mineru-router --worker-urls http://gpu0:8000,http://gpu1:8000

# Gradio Web UI
mineru-gradio

# VLM 推理服务（独立部署，用于多节点部署）
mineru-vllm-server      # vLLM 后端（Linux，推荐）
mineru-lmdeploy-server  # LMDeploy 后端（Windows）
mineru-openai-server    # OpenAI 兼容 API 后端

# 下载模型（交互式或带参数）
mineru-models-download
mineru-models-download -s huggingface -m all    # -s: huggingface|modelscope, -m: pipeline|vlm|all
```

### 测试
```bash
pytest tests/unittest/test_e2e.py                    # E2E 测试（需要 tests/unittest/pdfs/ 下的测试 PDF）
pytest tests/                                         # 全部测试（含根目录 test_*.py 文件）
pytest tests/test_make_blocks_to_content_list_2.py    # content_list_v2 专项测试
pytest tests/test_table_methods.py                    # 表格提取测试
pytest --cov=mineru --cov-report html                 # 带覆盖率（pyproject.toml 中已默认配置）
python tests/clean_coverage.py                        # 清理覆盖率报告

# 自定义模块验证
python -c "from mineru.utils.custom import *; print('import ok')"
```

覆盖率配置位于 `pyproject.toml` 的 `[tool.coverage.run]` 节——源码路径为 `mineru/`，已排除 CLI 入口文件。
测试 PDF 素材：`tests/unittest/pdfs/test.pdf`。

### 代码检查 / 类型检查
```bash
# CI 中未配置正式 linter。遵循 .claudecode-rules.md 规范：
# - Google 风格 docstring + 类型标注
# - 中文注释和 docstring
# - 使用 loguru logger（禁止 print()）
```

### CI（GitHub Actions）
- `cli.yml` — 在 master/dev 分支 push 时运行 `coverage run`（`mineru[test]`，超时 240 分钟）
- `python-package.yml` — tag 推送时构建 wheel，发布到 PyPI + GitHub Releases（Python 3.10-3.13 矩阵）
- `cla.yml` — PR 时强制 CLA 签署
- `mkdocs.yml` — 部署文档到 GitHub Pages
- `rerun.yml` — master 分支 CI 失败时自动重试（最多 3 次）
- 未配置 pre-commit hooks

### Docker
```bash
bash build-docker.sh                              # 构建镜像（mineru:custom），依赖变更时需重新构建
docker compose up -d                              # 启动服务（参见 compose.yaml）
docker compose restart                            # 代码修改后重启（无需重新构建——mineru/ 已通过卷挂载）
docker compose logs -f                            # 跟踪服务日志
docker compose logs --tail 50                     # 查看最近日志
docker compose exec mineru-zhangbo bash           # 进入容器 shell
curl http://localhost:8011/health                 # 健康检查
```

`docker/` 目录下的 Docker 变体：`compose.yaml`（通用）、`china/`（中国区配置）、`global/`（全球配置）。

`Dockerfile` 基于 `vllm/vllm-openai:v0.11.2`，安装 mineru `[core]`，运行时需要挂载模型到 `/models/pipeline` 和 `/models/vlm`（模型不打包进镜像）。入口点：`mineru-api`。

**Docker 开发工作流**：`compose.yaml` 将 `mineru/` 源码以卷方式覆盖安装包，因此代码修改只需 `docker compose restart`（无需重新构建）。仅当 `pyproject.toml` 依赖变更时才需重新构建（`bash build-docker.sh && docker compose up -d`）。

compose.yaml 关键路径：
- 容器名：`mineru-zhangbo`，端口 `8011:8000`
- 源码挂载：`/zhangbo/MinerU/mineru` → `/usr/local/lib/python3.12/dist-packages/mineru`
- 配置挂载：`/zhangbo/MinerU/mineru.json` → `/root/mineru.json`
- 输出挂载：`/zhangbo/MinerU/data` → `/vllm-workspace/output`
- 模型缓存：`/zhangbo/mineru_models` → `/root/.cache/modelscope`
- GPU：设备 `"2"`，`gpu-memory-utilization: 0.2`

## 架构

### 后端引擎系统

MinerU 拥有**三种本地后端**和两种对应的 HTTP 客户端后端，通过 `-b` / `--backend` 选择：

| 后端 | 说明 |
|---|---|
| `pipeline` | 传统 CV/OCR 管线：布局检测 → OCR → 公式/表格识别。需要 `mineru[pipeline]`（torch）。速度快，显存占用低。 |
| `vlm-engine` | 视觉语言模型（MinerU2.5-Pro）直接解析页面。需要 `mineru[vlm]`。复杂版面精度最高。 |
| `hybrid-engine` | **默认。** 组合 pipeline + VLM：VLM 做语义布局，pipeline 精炼 OCR/公式/表格，精度更高。 |
| `vlm-http-client` | 轻量客户端，连接远程 `mineru-api`（VLM 模式）。无需本地 torch。 |
| `hybrid-http-client` | 轻量客户端，连接远程 `mineru-api`（hybrid 模式）。 |

VLM 推理可由以下三种后端之一提供服务（自动检测或手动配置）：
- **vLLM**（Linux，默认）—— NVIDIA GPU 最高吞吐量
- **LMDeploy**（Windows）—— 替代推理引擎
- **MLX-VLM**（macOS/Apple Silicon）—— 通过 `mineru[mlx]`

### 智能路由系统（S0 → S1 → 引擎选择）

解析请求到达时，可选的 S0/S1 预处理管线自动选择最优引擎：

```
PDF 字节流
  → [S0] 文档质量分析（doc_quality.py）：DPI / 模糊 / 印章 / 旋转
  → [S1] 文档分类（doc_classifier.py）："信号灯"三路路由
      ├─ 🟢 DOCUMENT_PARSE：通用文档 → 按 OCR 难度选 vlm/hybrid/pipeline
      ├─ 🟡 STRUCTURED_TABLE：密集表格 → Hybrid + OCR 补充
      └─ 🔴 FORM_KVP：票据/卡证 → KVP Pipeline（见下文）
  → select_engine_route() 返回 EngineRoute（含 fallback 链）
```

S0/S1 管线默认**关闭**（`doc_type=auto` 时启用）。用户可通过 `doc_type` 参数显式指定类型跳过分类，或用 `-b` 强制指定后端跳过全部自动路由。

### KVP Pipeline（票据/卡证 KVP 提取）

当文档被分类为 `FORM_KVP` 时，走独立的 KVP 提取管线（不经过传统 mineru 后端）：

```
PDF 字节流
  → 逐页渲染为 PIL Image
  → KVP 引擎提取 Key-Value 对
  → _convert_kvp_to_middle_json() 转换为 MinerU 兼容格式
  → 输出标准 Markdown/JSON
```

三种运行模式：

| 模式 | engine 参数 | 说明 |
|---|---|---|
| 纯本地 | `pp-structure` | PaddleOCR + 空间距离配对 + 标签词典，**离线可用** |
| 本地 + VLM 纠错 | `pp-structure` + `verify_engine` | 本地提取为主，VLM 二次验证金额/姓名/日期等高价值字段 |
| 纯 VLM | `qwen-vl-max` / `qwen-vl-plus` | 远程 VLM API（需外网），精度最高 |

本地引擎（`kvp_local_engine.py`）支持 5 种文档布局：
- **A**：冒号分隔（"标签：值"）
- **B**：无分隔符拼接（正则拆分，如 "客户号10198594700"）
- **C**：网格布局（表头行 → 数据行，含 C0 预拆分阶段）
- **D**：表单布局（值在标签上方）
- **E**：同行左右配对

标签词典（`labels/`）按文档类型自动匹配：`deposit_slip`（存单）、`invoice`（发票）、`generic`（通用）。

关键环境变量：
- `KVP_ENGINE`：默认 KVP 引擎（默认 `pp-structure`）
- `KVP_SERVER_URL`：自定义 VLM API 地址
- `KVP_VERIFY_ENGINE`：VLM 纠错引擎（如 `qwen-vl-max`）

### 调度层

`mineru` CLI 是一个瘦客户端，未指定 `--api-url` 时会自动启动本地 `mineru-api` 子进程。架构：

```
CLI (client.py) → mineru-api (fast_api.py) → 后端引擎 → 输出
                    ↑
mineru-router (router.py) — 多服务负载均衡，API 兼容
mineru-gradio (gradio_app.py) — Web UI
```

关键端点：`POST /file_parse`（同步）、`POST /tasks`（异步提交）、`GET /tasks/{task_id}`（轮询状态）、`GET /tasks/{task_id}/result`（下载结果）、`GET /health`（健康检查，含队列统计）。

### API 流程

```
POST /tasks (202) → GET /tasks/{task_id}（轮询状态）→ GET /tasks/{task_id}/result (200 + zip)
POST /file_parse → 等待完成，直接返回结果
```

异步任务在 `task_retention_seconds` 后自动过期（默认：1800 秒，可通过 `MINERU_TASK_RETENTION_SECONDS` 配置）。

### 数据流

```
PDF/图片/DOCX/PPTX/XLSX
  → [S0] 质量分析（可选）：DPI/模糊/印章/旋转检测
  → [S1] 文档分类（可选）：通用 / 密集表格 / KVP 表单
  → [路由] 按分类选择引擎：
      ├─ 🟢 通用 → pipeline / vlm / hybrid 后端
      ├─ 🟡 表格 → hybrid + OCR 补充
      └─ 🔴 表单 → KVP Pipeline（本地 OCR / VLM / 混合）
  → [middle_json] 统一中间表示（每页 blocks + spans）
  → [生成内容] 转换为目标格式（Markdown、JSON、content_list）
  → 输出（Markdown + 图片 + 布局可视化）
```

### 模块地图

- `mineru/backend/<engine>/` — 各引擎的分析 + 模型转 JSON + 内容生成管线
  - `hybrid/hybrid_analyze.py` — hybrid：VLM 布局 + pipeline OCR/公式精炼
  - `pipeline/batch_analyze.py` — pipeline：批量 layout/MFR/OCR/table 模型
  - `vlm/vlm_analyze.py` — VLM：MinerUClient 页面推理
  - `office/` — 原生 DOCX/PPTX/XLSX 解析（无需 PDF 转换）
- `mineru/model/` — 独立模型封装：`layout/`、`mfr/`（公式识别）、`ocr/`、`table/`、`vlm/`、`docx/`、`pptx/`、`xlsx/`
- `mineru/cli/` — 入口点：`client.py`、`fast_api.py`、`router.py`、`gradio_app.py`、`common.py`（共享解析逻辑）
- `mineru/data/` — I/O：`FileBasedDataWriter`、S3 读写器（惰性导入 `boto3`）
- `mineru/utils/` — 共享工具：`pdf_image_tools.py`、`ocr_utils.py`、`config_reader.py`、`enum_class.py`
- `mineru/utils/custom/` — 团队自定义扩展：S0/S1 智能路由、KVP Pipeline、表格后处理、PDF 旋转修正等（详见下方自定义代码约定）

### 关键枚举（`mineru/utils/enum_class.py`）

- `BlockType` — 文档块分类（TEXT、TITLE、IMAGE、TABLE、INTERLINE_EQUATION、CODE、HEADER、FOOTER 等）
- `ContentType` / `ContentTypeV2` — 输出内容类型，用于 Markdown 渲染
- `MakeMode` — 输出格式：`mm_markdown`、`nlp_markdown`、`content_list`、`content_list_v2`
- `ModelPath` — 模型下载路径（HuggingFace / ModelScope）

### 配置

- **配置文件：** `~/mineru.json`（可通过 `MINERU_TOOLS_CONFIG_JSON` 环境变量覆盖路径）。模板：`mineru.template.json`。
- **关键环境变量：** `MINERU_MODEL_SOURCE`（local/modelscope/huggingface）、`MINERU_DEVICE_MODE`（cuda/cpu/npu/mps）、`MINERU_BACKEND`、`MINERU_LOG_LEVEL`
- **VLM 功能开关：** `MINERU_VLM_FORMULA_ENABLE`（默认：true）、`MINERU_VLM_TABLE_ENABLE`（默认：true）
- **性能：** `MINERU_PROCESSING_WINDOW_SIZE`（默认：64，并发处理页数）、`CUDA_VISIBLE_DEVICES`
- **API 服务：** `MINERU_API_OUTPUT_ROOT`（输出目录）、`MINERU_API_DISABLE_ACCESS_LOG`（禁用 uvicorn 访问日志）、`MINERU_API_ENABLE_VLM_PRELOAD`（启动时预加载 VLM）
- **KVP Pipeline：** `KVP_ENGINE`（默认 KVP 引擎，默认 `pp-structure`）、`KVP_SERVER_URL`（自定义 VLM API 地址）、`KVP_VERIFY_ENGINE`（VLM 纠错引擎）
- **配置文件节：** `models-dir`（pipeline/vlm 路径）、`s3`（存储桶配置）、`latex-delimiters`、`llm-aided`（LLM 辅助标题分类）

### 中央分发（`mineru/cli/common.py`）

`do_parse()` / `aio_do_parse()` 是中央路由函数，分发到正确的后端：
1. Office 文件（docx/pptx/xlsx）→ `office_*_analyze` 直接处理（无需 PDF 转换）
2. `pipeline` 后端 → `PipelineMagicModel`（本地）
3. `vlm-*` 后端 → `VlmMagicModel`（本地）或 VLM HTTP 客户端（远程）
4. `hybrid-*` 后端 → `HybridMagicModel`（本地，需要 `torch`）或 hybrid HTTP 客户端（远程）
5. 客户端后端（`*-http-client`）连接远程 `mineru-api`——无需本地 torch

**后端名称解析**：`-b` 参数值（如 `vlm-vllm-engine`、`hybrid-auto-engine`）首先检查 `vlm-` / `hybrid-` 前缀以确定分发路径。然后剥离前缀（`vlm-` 为 4 字符，`hybrid-` 为 7 字符），剩余部分作为引擎名称传递。`auto-engine` 通过 `get_vlm_engine()` 触发自动检测。

**同步 vs 异步**：`vllm-engine` 仅同步，`vllm-async-engine` 仅异步。使用错误的引擎会抛出异常。

## 自定义代码约定（`.claudecode-rules.md` 第 11 节）

本项目是基于 `opendatalab/MinerU` 的 **Fork 项目**。为尽量减少与上游的合并冲突：

1. **新增自定义代码** → `mineru/utils/custom/`（上游无 `custom/` 目录——零冲突）
2. **上游文件中的 Hook** → 最小化的 `try/except import` 代码块，用 `[自定义]` 注释标记
3. 保持 hook 小巧且隔离；所有逻辑位于 custom 模块中

当前自定义模块：
| 模块 | 用途 |
|---|---|
| `mineru/utils/custom/pdf_utils.py` | `generate_rotation_corrected_pdf()` — PDF 旋转修正 |
| `mineru/utils/custom/content_list_utils.py` | `enrich_list_items_with_bbox()` — content_list_v2 中每个 list_item 独立 bbox |
| `mineru/utils/custom/table_utils.py` | `split_merged_table_cells()`、`split_summary_from_data_cell()`、`normalize_table_colspan()`、`normalize_invoice_table()` — VLM 表格后处理全套管道 |
| `mineru/utils/custom/doc_quality.py` | `DocumentQuality`、`analyze_document_quality()` — S0 文档质量分析（DPI/模糊/印章/旋转检测） |
| `mineru/utils/custom/doc_classifier.py` | `DocType`、`classify_document()` — S1 文档分类器（"信号灯"三路路由：通用/表格/KVP 表单） |
| `mineru/utils/custom/engine_factory.py` | `EngineRoute`、`select_engine_route()` — 引擎工厂 + 策略选择器（自动选择最优解析引擎） |
| `mineru/utils/custom/kvp_extractor.py` | `extract_kvp_from_form()` — KVP Pipeline 主入口（本地 OCR + VLM + 混合纠错） |
| `mineru/utils/custom/kvp_local_engine.py` | `extract_kvp_local()` — 本地 KVP 提取引擎（PaddleOCR + 空间距离配对 + 可插拔标签词典） |
| `mineru/utils/custom/labels/` | KVP 标签词典注册表（`deposit_slip.py`、`invoice.py`、`generic.py`），按文档类型自动匹配 |

将上游 master 合并到 `develop` 时，冲突主要局限于这些标注的 hook 点。

### 自定义 Hook 点（上游合并时需关注的文件）

以下文件包含 `[自定义]` hook 点。上游合并时，**以 hook 代码为准**解决冲突：

| 文件 | 大致行号 | Hook 用途 |
|---|---|---|
| `mineru/cli/common.py` | ~260 | 输出时生成旋转修正后的 PDF |
| `mineru/cli/common.py` | ~641, ~1035 | S0 → S1 智能路由系统（`_try_smart_routing` / `do_parse`） |
| `mineru/cli/fast_api.py` | ~164, ~867 | 多引擎路由参数（`AsyncParseTask` / `run_parse_job`） |
| `mineru/cli/fast_api.py` | ~460, ~522 | KVP Pipeline 回退（标准目录不存在时检查 kvp/ 目录） |
| `mineru/cli/fast_api.py` | ~607 | ZIP 下载中包含 `_rotated.pdf` |
| `mineru/cli/api_request.py` | ~38, ~171, ~240 | KVP 多引擎路由参数（`doc_type` / `kvp_engine` / `kvp_verify_engine`） |
| `mineru/backend/vlm/vlm_middle_json_mkcontent.py` | ~59 | VLM 表格 HTML 后处理：拆分合并单元格 + 合计标签分离 + 发票表格 colspan 规范化与缺失值推断 |
| `mineru/backend/vlm/vlm_middle_json_mkcontent.py` | ~915 | content_list_v2 中为 list_items 注入独立 bbox |
| `mineru/backend/hybrid/hybrid_model_output_to_middle_json.py` | ~262 | Hybrid 模式使用 Pipeline OCR 补充 VLM 表格空单元格 |
| `mineru/backend/hybrid/hybrid_model_output_to_middle_json.py` | ~275 | Hybrid 模式 Image 块 OCR 回退（VLM 误判为 image 的区域做 OCR 兜底） |
| `mineru/utils/pdf_image_tools.py` | ~601 | 旋转检测使用 `PaddleOrientationClsModel`（上游更换了模型类） |
| `mineru/backend/hybrid/hybrid_analyze.py` | ~737, ~886 | 解析入口处对 PDF 做整体旋转修正后再打开（同步/异步路径） |
| `mineru/backend/vlm/vlm_analyze.py` | ~442, ~542 | 解析入口处对 PDF 做整体旋转修正后再打开（同步/异步路径） |

## Plan 归档机制

每次使用计划模式（`EnterPlanMode` → `ExitPlanMode`）后，**必须在对话结束前**将产生的 plan 文件归档到项目目录。

**执行时机**：`ExitPlanMode` 获得用户批准后，或对话即将结束时。

**归档操作**：
```bash
# 按日期建立子目录，同一天的 plan 文件放在同一目录下
date_dir=$(date +%Y-%m-%d)
mkdir -p agents_logs/plans/${date_dir}
# 命名规则：日期_描述性名称.md（如 2026-07-29_kvp-独立span-bbox.md）
# 描述需简洁反映 plan 的核心内容，不使用 Claude 内部随机名
latest=$(ls -t /root/.claude/plans/*.md 2>/dev/null | head -1)
if [ -n "$latest" ]; then
    # 取描述性名称 — 由执行者根据 plan 内容手动命名
    desc_name="<核心描述>.md"
    cp "$latest" "agents_logs/plans/${date_dir}/${date_dir}_${desc_name}"
    echo "已归档: agents_logs/plans/${date_dir}/${date_dir}_${desc_name}"
fi
```

**元数据要求**：归档后使用 Edit 工具在文件**头部插入**以下可见的元数据节：

```markdown
> **归档时间**: YYYY-MM-DD HH:MM
> **来源文件**: <原始 Claude plan 文件名>
> **项目版本**: 4.2.0
> **Git 分支**: <当前分支>
> **原因**: <一句话说明为什么要做这个 plan>
> **背景**: <问题背景，2-3 句话>
> **结论**: <plan 达成的决策或实施方案概要>

---
```

**再次强调**：此操作必须在每次对话结束前完成。归档文件缺乏元数据或有遗漏时，应在下次对话中补全。

## `.claudecode-rules.md` 关键规则摘要

完整规范（约 800 行，中文）包含 12 个章节。本项目代码的关键规则：

- 所有注释、docstring、日志必须使用**简体中文**
- 所有新增函数必须包含 Google 风格 docstring（`Args:`、`Returns:`）+ 类型标注
- 使用 `logger`（loguru）——**禁止** `print()`
- 异常必须使用 `logger.exception()`（禁止 `except: pass`）
- **优先最小改动**和向后兼容，而非大规模重构
- 保持原有代码风格；不要自动格式化或整理 import
- 未经明确要求，禁止升级依赖
- 输出完整、可直接复制的代码块——禁止 `# 其它代码保持不变` 式的省略

## 开发者文档

`docs/zh/dev/` 下的中文架构文档：
- `项目架构全览.md` — 架构总览
- `架构说明.md` — 架构说明
- `接口说明.md` — API/接口文档
- `文件内容说明.md` — 文件内容指南

## Git 工作流

- `develop` — 团队工作分支（在上游基础上叠加自定义修改）
- `master` — 上游跟踪分支（`opendatalab/MinerU`）
- 合并：`upstream/master` → `develop`（冲突时以团队修改为准）
- Fork 版本号：`mineru/version.py` → `__version__ = "4.2.0"`（与上游 3.4.0 区分）
