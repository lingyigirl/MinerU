> **归档日期**: 2026-07-29 11:07
> **来源文件**: post-file-parse-files-transient-cerf.md
> **项目版本**: 4.2.0
> **Git 分支**: feature/kvp-local-kie
> **原因**: KVP 输出格式补全（每字段独立 span+bbox）+ Plan 自动归档机制
> **背景**: 1. 上次归档遗漏且缺乏元数据 2. KVP 输出只有整页 bbox，与 hybrid-auto 输出格式不一致
> **结论**: 每字段生成独立 span（bbox 来自 OCR），更新 CLAUDE.md/.claudecode-rules.md 归档规则，写入 memory 提醒

---

# KVP 输出格式补全 + Plan 自动归档机制

## Context

**问题 1 - Plan 未归档**：上一次回答结束后未将 `/root/.claude/plans/` 下的 plan 文件复制到 `agents_logs/plans/`。且已归档的 md 文件缺乏元数据（日期、版本、分支、原因、背景、结论），不利于后续查阅。

**问题 2 - KVP 输出无详细 bbox**：当前 `_convert_kvp_to_middle_json()` 只生成一个巨型 span，bbox 为 `[0, 0, w, h]`（整页）。而原 hybrid-auto-engine 的 middle_json 中每个文本块/字段都有独立的 bbox。KVP 引擎内部已有 OCR box 的 bbox 信息，只需透传。

## 问题 1 方案：Plan 归档自动化 + 元数据模板

### 1.1 修改 `.claudecode-rules.md` 第 12.7 节

- 将归档要求明确为 **CLAUDE.md 系统级指令**（每次会话加载）
- 归档文件必须包含元数据 header 模板
- 归档时机从"ExitPlanMode 后"改为"每次对话产出的 plan 文件在对话结束前必须归档"

### 1.2 归档文件格式

每个归档的 plan md 文件头部必须添加以下元数据块：

```markdown
<!--
归档日期: 2026-07-29 10:42:35
来源文件: post-file-parse-files-transient-cerf.md
项目版本: 4.2.0
Git分支: feature/kvp-local-kie
归档原因: KVP Pipeline 三个优化点
背景: 基于 hybrid-http-client 调用的流程分析发现 bug
结论: backend 覆盖添加 WARNING / S0 日志提升 INFO / OCR 单例复用
-->
```

### 1.3 写入 CLAUDE.md 的记忆指令

在 CLAUDE.md 添加明确指令：每次调用 ExitPlanMode 并收到用户批准后，必须立即将 `/root/.claude/plans/` 下对应的 plan 文件归档到 `agents_logs/plans/` 并添加元数据 header。

## 问题 2 方案：KVP 每字段独立 span + bbox

### 2.1 修改架构

```
当前：_pair_kvp → {label: value} → _convert  → 1个span [0,0,w,h]
修复：_pair_kvp → {label: {value, bbox}} → _convert → N个span 各有bbox
```

### 2.2 修改文件清单

| 文件 | 改动 |
|------|------|
| `mineru/utils/custom/kvp_local_engine.py` | `_pair_kvp` 和 `_pre_split_grid_values` 记录每个字段的 label_bbox + value_bbox；`extract_kvp_local` 返回附加 `_kvp_bboxes` 的 dict |
| `mineru/utils/custom/kvp_extractor.py` | `_convert_kvp_to_middle_json` 根据 `_kvp_bboxes` 为每个字段创建独立 span（含 bbox） |
| `.claudecode-rules.md` | 更新 12.7 节：归档自动化 + 元数据模板 |
| `CLAUDE.md` | 添加 Plan 归档指令 |

### 2.3 数据流变更

**变更前**：
```python
extract_kvp_local() → {"户名": "陈汉武", "客户号": "10198594700", ...}
```

**变更后**：
```python
extract_kvp_local() → {
    "户名": "陈汉武",
    "客户号": "10198594700",
    ...,
    "_kvp_bboxes": {
        "户名": {"label_bbox": [300, 578, 407, 644], "value_bbox": [413, 518, 586, 626]},
        "客户号": {"label_bbox": [942, 655, 1351, 757], "value_bbox": [942, 655, 1351, 757]},
        ...
    }
}
```

**span 生成变更**：
```python
# 变更前：1个大span
spans = [{"bbox": [0, 0, w, h], "text": "户名: 陈汉武\n客户号: ..."}]

# 变更后：每字段1个span，各带自己的合并bbox
spans = [
    {"bbox": [300, 518, 586, 644], "text": "户名: 陈汉武", "type": "text",
     "kvp_label": "户名", "kvp_value": "陈汉武"},
    {"bbox": [942, 655, 1351, 757], "text": "客户号: 10198594700", "type": "text",
     "kvp_label": "客户号", "kvp_value": "10198594700"},
    ...
]
```

### 2.4 bbox 计算规则

- 有独立 label box + value box → `merged_bbox = [min(lx1, vx1), min(ly1, vy1), max(lx2, vx2), max(ly2, vy2)]`
- 冒号分隔 → 整个 box 的 bbox 直接作为 merged_bbox
- 网格拆分 → 按拆分后的字符比例估算 bbox
- bbox 值为 **页面绝对坐标**（像素），来自 OCR box 坐标

## 验证

```bash
docker compose restart && sleep 40
curl -X POST http://172.19.0.3:8011/file_parse \
  -F "files=@定期存单.pdf" -F "doc_type=form_kvp" \
  -F "kvp_engine=pp-structure" -F "return_middle_json=true"
# 检查 middle_json 中每个 span 是否有独立的 bbox
# 检查 agents_logs/plans/ 中是否有本次归档的 plan 文件
```
