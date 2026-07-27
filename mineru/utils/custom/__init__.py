# 自定义扩展模块
#
# 此目录存放团队对 MinerU 的自定义改造代码。
# 将自定义代码放在这里而非直接修改上游文件，可以减少与开源 MinerU 合并时的冲突。
#
# 使用原则：
# 1. 新增功能/函数放在此目录下的对应模块中
# 2. 对上游文件的修改仅限于最小化的 hook 点（try/except import + 调用）
# 3. 所有自定义代码需添加清晰的注释说明用途
#
# 当前模块：
# - pdf_utils.py: PDF 旋转修正功能（generate_rotation_corrected_pdf）
# - content_list_utils.py: content_list_v2 后处理（list_item 独立 bbox）
# - table_utils.py: VLM 表格 HTML 后处理（split_merged_table_cells, split_summary_from_data_cell,
#   normalize_table_colspan, normalize_invoice_table, supplement_empty_table_cells,
#   supplement_vlm_table_cells_with_ocr）
# - doc_quality.py: S0 文档质量分析器（DPI/模糊/印章/旋转检测）
# - doc_classifier.py: S1 文档分类器（"信号灯"三路路由：通用/表格/表单）
# - kvp_extractor.py: KIE Pipeline（基于 MLLM 的票据/卡证 KVP 信息提取）
# - engine_factory.py: 引擎工厂 + 策略选择器（自动选择最优解析引擎）
