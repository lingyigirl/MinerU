"""VLM 表格 HTML 后处理工具（包）。

针对 VLM 模型（MinerU2.5-Pro）在 hybrid 后端生成的表格 HTML 中，
数据行被错误地合并为单个 colspan 单元格的问题（如增值税发票中的
"项目名称 规格型号 ... 税额 数据 数据 ..."），进行自动检测和拆分。

上游合并时此模块仅需保留，无需修改。

本包由原单文件 table_utils.py 按功能域拆分而来，子模块分层如下
（下层不得反向依赖上层）：

    _common.py        共享基础工具与关键词常量
    detect.py         表格类型检测门控
    merge_split.py    合并单元格拆分引擎
    summary.py        合计/小计摘要行拆分
    invoice.py        增值税发票 / VAT 专用后处理
    ocr_guards.py     鬼影表与印章噪声守卫
    ocr_align.py      OCR 网格构建、行对齐与列映射
    ocr_fill.py       行重建与空单元格填充核心
    header_prefix.py  表头前缀提取与 ¥ 对齐
    header_consensus.py 跨页表头一致性归一化（守卫 11 扩展版，双向污染 + 印章校验）
    amount_sep_repair.py 金额千分位逗号被读成点号的确定性回写（列级金额投票闸门）
    ocr_supplement.py 印章采集与 VLM 表格 OCR 补充编排

调用链【阶段 B：内容生成钩子】入口 _format_embedded_html
（vlm_middle_json_mkcontent.py），按固定顺序串行调用：
  1. split_merged_table_cells        全行/局部合并单元格拆分
  2. split_summary_from_data_cell    数据行内嵌「合计」拆分 + rowspan 处理
  3. extract_column_header_prefixes  表头前缀提取 / ¥ 对齐
  4. normalize_invoice_table         发票专用规范化入口，内部依次：
       _normalize_vat_invoice_columns  8 列签名归一化（名称/单价 colspan 2→1）
       _format_summary_row_colspan     合计行连续空单元格合并
       _infer_missing_values_in_table  缺失税率推断（默认关闭）
  5. fix_summary_row_yen_position    合计行 ¥/￥ 值列对齐
  6. split_info_cell_multiline       购买方/销售方信息多行拆分

调用链【阶段 A：Hybrid OCR 补充】入口 finalize_middle_json
（hybrid_model_output_to_middle_json.py）：
  supplement_vlm_table_cells_with_ocr  VLM 表格空单元格 OCR 补充 / 拼接行确定性重建
  supplement_empty_table_cells         通用空单元格 OCR 补充
  repair_dotted_amount_separators     金额千分位点号回写（VLM 天然缺陷，列级投票闸门；
                                       hybrid 与 VLM 后端同调，早于
                                       build_para_blocks_from_preproc）

【发票检测与归一化】
  _is_invoice_table                发票表级检测（≥3 关键词）
  _normalize_vat_invoice_columns   8 列签名收拢（专用/普通发票）
  _is_structurally_sparse_table / _is_financial_statement_table  反向门控

【共享常量】（关键词集合语义有重叠，维护时注意同步）
  _INVOICE_HEADER_KEYWORDS        通用发票表头关键词
  _SPLIT_SUMMARY_KEYWORDS         合计/小计/总计摘要关键词
  _INVOICE_DETECTION_KEYWORDS     发票检测关键词
  _INVOICE_DATA_COLUMN_KEYWORDS   数据列关键词
  _NUMERIC_COLUMN_KEYWORDS        数值列关键词
  _VAT_INVOICE_NAME_LABELS        货物区名称列标签
  _VAT_INVOICE_COLUMN_SIGNATURE   8 列签名（除名称外 7 列）
  _VAT_INVOICE_ROW_LABELS         非货物区行级标签

历史导入路径 from mineru.utils.custom.table_utils import X 全部保持可用。
"""

# 子模块符号统一上提到包命名空间，保持历史导入路径可用：
#   from mineru.utils.custom.table_utils import X
from mineru.utils.custom.table_utils._common import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.detect import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.merge_split import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.summary import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.invoice import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.ocr_guards import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.ocr_align import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.ocr_fill import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.header_prefix import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.header_consensus import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.amount_sep_repair import *  # noqa: F401,F403
from mineru.utils.custom.table_utils.ocr_supplement import *  # noqa: F401,F403

# 子模块本身也挂到包上，便于按域直接引用与调试
from mineru.utils.custom.table_utils import (  # noqa: F401
    _common,
    detect,
    merge_split,
    summary,
    invoice,
    ocr_guards,
    ocr_align,
    ocr_fill,
    header_prefix,
    header_consensus,
    amount_sep_repair,
    ocr_supplement,
)


def _collect_all() -> list:
    """汇总全部子模块的导出符号，构成包级 __all__。"""
    modules = [
        _common,
        detect,
        merge_split,
        summary,
        invoice,
        ocr_guards,
        ocr_align,
        ocr_fill,
        header_prefix,
        header_consensus,
        amount_sep_repair,
        ocr_supplement,
    ]
    names: list = []
    for module in modules:
        names.extend(getattr(module, "__all__", []))
    return names


__all__ = _collect_all()
