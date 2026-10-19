"""解析服务: PDF/MD → 规范文档模型（契约见 matrag.types）。

provider 可插拔:
- ``geometry.py``     —— local 几何草稿（PyMuPDF 单元格矩形、合并 span）
- ``markdown_reader.py`` —— Markdown 草稿（表格已是干净 pipe 表）
- ``reconstruct.py``  —— 合并单元格 LLM 重建（去规范化/平铺叶表头/`<br>`）
- ``verify.py``      —— 对账闸门（维度/空值/凭空字符/merge 统计；不过 = fail-closed）

后续外部解析器（docling/MinerU/Mathpix…）作为新 provider 接入，过同一套 verify 基准。
"""
