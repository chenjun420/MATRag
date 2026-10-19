"""表格重建 Prompt 模板（P0-4）。

只放字符串模板与序列化辅助，不含 IO / 不 import contracts 之外的东西。
``build_system_prompt`` / ``build_user_prompt`` 产出喂给 LLM 的 system+user；
``JSON_SCHEMA_HINT`` 作为 ``complete_json`` 的 schema_hint 约束输出形状。
"""

from __future__ import annotations

from matrag.types import TableDraft

#: ``complete_json`` 用的输出形状约束（只输出 JSON，无解释）。
JSON_SCHEMA_HINT = """{
  "headers": ["<唯一叶表头，多级用 / 连接>", ...],
  "rows": [["<字符串单元格，换行用 <br> 表示>", ...], ...],
  "marks": ["<合并/平铺痕迹，如 rowspan:3@col0>", ...],
  "confidence": 0.0
}"""

_SYSTEM_TEMPLATE = """\
你是电源/电子规格书表格重建专家。给你一份从 PDF 几何解析得到的表格草稿
（密集网格 + 每个单元格的原文 + 合并跨度 + 周边阅读顺序原文），
你要输出重建后的规范表格 JSON。

# 去规范化规则（严格遵守）
1. 纵向合并（row_span > 1）：合并格的值必须【向下重复】到它覆盖的每一个行。
2. 横向合并（col_span > 1）：合并格的值必须【向右重复】到它覆盖的每一个列。
   特例：若某【数据行】的合并格横向覆盖整行（col_span == n_cols），
   该值只写入第一个列，其余列填 "-"（marks 仍记录该 colspan，避免整行重复 6 遍的噪声）。
3. 多级表头：同一列的多级表头用 "/" 从上到下拼接成一个唯一叶表头，
   例如 "项目/输入欠压保护/欠压点"。
4. 单元格内的换行一律替换为 "<br>"（不要保留真实换行符）。
5. 绝对不得新增源文本中不存在的数字或单位；不得凭空造词。
6. 不得出现 "Col2"、None、"nan"、"" 之类占位符；源里确无文本的格填 "-"。
7. 列数以草稿 n_cols 为准，行数以草稿 n_rows 为准；输出必须矩形。

# 表头行判定（无表头表格很常见，如"标签-值"型签署表/封面表/声明表）
- 草稿第 0 行只有在语义上是表头时才可作表头（列含义/单位/测试条件等）。
- 若第 0 行是数据（如 "Prepared by/拟制/陈明" 这类标签-值排列，或签署名单），必须：
  a) 全部行（含第 0 行）都当作数据输出；
  b) 按列语义自拟简短表头（如 "项目/内容/日期"），严禁输出 Col1/Col2 占位，也严禁把数据行原样搬进 headers；
  c) marks 追加一条 "no_header_row"。
- 自拟表头同样必须满足下述唯一性要求。

# 表头唯一性（硬性）
- headers 内不允许重复。多级拼接后仍重复时追加限定词
  （如 "欠压点(输入欠压保护)" / "恢复点(输入欠压保护)"），保证全表唯一。

# 痕迹 marks
- 对草稿中【每一个】合并单元格输出恰好一条痕迹：
  行合并写 "rowspan:{跨度}@col{起始列}"，列合并写 "colspan:{跨度}@row{起始行}"，
  行列同时合并写 "rowspan:{r}@col{c};colspan:{s}@row{r}"。
- 发生过表头平铺时追加一条 "flattened_header"。
- marks 里 rowspan/colspan 痕迹的总数必须等于草稿的合并单元格数。

# 输出
只输出一个 JSON 对象，包含 headers / rows / marks / confidence 四个键。

# Few-shot 示例
草稿说明：n_rows=5, n_cols=3；前 2 行是两级表头（"项目" 横跨 2 列，
"备注" 纵跨 2 行），后 3 行是数据（"输入欠压保护" 纵跨 3 行，
子项为 欠压点/恢复点/回差）。网格草稿（[row][col] (row_span,col_span) 原文）：

[0][0] (1,2) "项目"
[0][1] None
[0][2] (2,1) "备注"
[1][0] (1,1) "项目名称"
[1][1] (1,1) "限值"
[1][2] None
[2][0] (3,1) "输入欠压保护"
[2][1] (1,1) "欠压点"
[2][2] (1,1) "-"
[3][0] None
[3][1] (1,1) "恢复点"
[3][2] (1,1) "-"
[4][0] None
[4][1] (1,1) "回差"
[4][2] (1,1) "-"

期望 JSON 输出（叶表头平铺且唯一；去规范化后数据 3 行 x 3 列，
"输入欠压保护" 已向下重复到 3 行；合并痕迹共 3 条）：

{
  "headers": ["项目/项目名称", "项目/限值", "备注"],
  "rows": [
    ["输入欠压保护", "欠压点", "-"],
    ["输入欠压保护", "恢复点", "-"],
    ["输入欠压保护", "回差", "-"]
  ],
  "marks": ["colspan:2@row0", "rowspan:2@col2", "rowspan:3@col0", "flattened_header"],
  "confidence": 0.95
}
"""

_USER_TEMPLATE = """\
请重建下面这份表格草稿。

# 元信息
- caption: {caption}
- n_rows: {n_rows}, n_cols: {n_cols}
- 合并单元格数: {merged_cells}

# 网格草稿（[row][col] (row_span,col_span) 原文；None = 被合并覆盖的槽）
{grid_block}

# 周边阅读顺序原文（供判读，可不含全部表格内容）
{reading_text}

请按系统提示的规则输出重建后的 JSON。\
"""


def serialize_grid(draft: TableDraft) -> str:
    """把草稿密集网格序列化成每格一行的可读文本，供 user prompt 使用。

    每行给出 ``[row][col] (row_span,col_span) 原文``；被覆盖的 None 槽也列出，
    让模型能看到合并结构。仅输出单元格非空原文，空文本给 ""。
    """
    lines: list[str] = []
    for r in range(draft.n_rows):
        for c in range(draft.n_cols):
            cell = draft.grid[r][c] if r < len(draft.grid) and c < len(draft.grid[r]) else None
            if cell is None:
                lines.append(f"[{r}][{c}] None")
            else:
                text = cell.text.replace("\n", "\\n")
                lines.append(
                    f"[{r}][{c}] ({cell.row_span},{cell.col_span}) {text!r}"
                )
    return "\n".join(lines)


def build_system_prompt() -> str:
    """返回系统提示（角色 + 去规范化规则 + few-shot 示例）。"""
    return _SYSTEM_TEMPLATE


def build_user_prompt(draft: TableDraft) -> str:
    """返回用户提示（元信息 + 网格序列化 + reading_text）。"""
    return _USER_TEMPLATE.format(
        caption=draft.caption or "(无标题)",
        n_rows=draft.n_rows,
        n_cols=draft.n_cols,
        merged_cells=draft.merged_cells,
        grid_block=serialize_grid(draft),
        reading_text=draft.reading_text.strip() or "(无)",
    )
