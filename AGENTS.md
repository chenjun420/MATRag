# 代码约定（所有任务与子代理必须遵守）

- Python 3.13，`src/matrag/` 布局；导入一律 `from matrag.xxx import yyy`。
- **公共契约文件非本人任务不得修改**：`src/matrag/types.py`、`config.py`、`llm.py`、`pyproject.toml`、`TODO.md`。发现契约不够用，在收尾报告里写明所需改动，由主会话统一改。
- 测试在 `tests/`，与被测模块同名镜像（`src/matrag/parse/x.py` → `tests/test_parse_x.py`）；一律可离线跑（LLM/网络用 stub），真实调用的用例按 `pytest.mark.skipif(not settings.live, ...)` 跳过。
- **依赖已预装，禁止 `uv run`/`uv sync`/`uv add`**（并行任务下 uv 重解析环境会互相冲突）；需要新依赖在报告中说明。命令统一用 venv 直调：
  - 测试: `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`
  - ruff: `.venv\Scripts\python.exe -m ruff check src tests`
- 风格：docstring/注释中文；类型标注完整；异常用 `matrag.types` 里定义的或标准库；失败 fail-fast，**不得静默降级吞错**（降级必须留痕并可查询）。
- 收尾必做：`.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` 全绿 + `.venv\Scripts\python.exe -m ruff check src tests` 无 E/F。
- 报告格式：改了哪些文件/新增接口/测试结果/遗留风险。
