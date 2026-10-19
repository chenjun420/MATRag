# tools/contract_sync — 跨仓契约同步门 (Phase A.1)

MATRag 与 ATEStudio 两仓之间靠**词表**与**契约指纹**维持诚实。任一侧漂移,
对应的测试会静默缺失或字段静默丢失, 计划照常生成、报告照样变绿, 流出的却是
不合格品。本工具把这种漂移变成一次 **CI 红灯**。

## 组成

| 脚本 | 作用 |
|---|---|
| `check_vocab.py` | 校验 `KIND_UNITS` / 绑定表 / 方法目录 / `DRIVER_TABLE` 三者对齐 |
| `check_contract_hash.py` | 校验两侧 `contract_hash()` 与 `KNOWN_CONTRACT_HASH` 一致 |

## 数据源

`check_vocab.py` 读取四个来源:

1. `MATRag/src/matrag/extract/kinds.py:KIND_UNITS` — 生产侧 25 个 `(kind, side)`;
2. `MATRag/data/domain/source/power_test_methods.yaml` — 生产侧测试方法目录;
3. `$ATE_STUDIO_ROOT/config/test_method_bindings.yaml` — 消费侧绑定表;
4. `$ATE_STUDIO_ROOT/src/ate_cloud/services/aterag_script_emit.py:DRIVER_TABLE`
   — 消费侧驱动派发表 (AST 静态解析, 不 import 消费侧包)。

校验关系 (每条带严重级别: `FAIL` 硬门禁 / `WARN` 软警告 / `INFO` 信息):

- `#1 [FAIL]` `KIND_UNITS` ↔ 绑定表 `(kind, side)` **双向差集** (两方向都必须为空);
- `#2 [WARN]` `power_test_methods.yaml` 的 `method_id` (MATRag 内部方法库, `M-*`) 与
  绑定表 `method_ref` (外部标准 id, 如 `iec62368_*`) 的**命名空间重叠报告** ——
  二者设计上就是两个不相交的命名空间 (内部方法目录 vs 外部标准溯源), 仅报告差异,
  **不阻断 CI**;
- `#3 [FAIL]` 绑定表的 `action` ⊆ `DRIVER_TABLE` (`DRIVER_TABLE` 中未被引用的 action
  仅作信息提示, 不阻断);
- `#4 [INFO]` `method_ref` 值是否符合外部标准命名约定 (启发式正则)。

**只有 `FAIL` 级别失败才计入退出码 1**。

`check_contract_hash.py` 读取:

- `MATRag/src/matrag/extract/bundle.py:KNOWN_CONTRACT_HASH` (本进程 import);
- `$ATE_STUDIO_ROOT/src/ate_cloud/schemas/aterag_bundle.py:KNOWN_CONTRACT_HASH` (文本解析);
- 两侧 `contract_hash()`: 生产侧本进程内计算, 消费侧用其 venv 解释器在
  **子进程**里计算 (避免本进程 import `ate_cloud` 触发其运行时初始化)。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ATE_STUDIO_ROOT` | `../ATEStudio` | ATEStudio 仓库根目录 (相对 MATRag 根目录或绝对路径) |
| `ATE_STUDIO_PYTHON` | `<ate_root>/.venv` | 可 import `ate_cloud` 的解释器; 未设时自动探测 `<ate_root>/.venv/Scripts/python.exe` (Windows) 或 `<ate_root>/.venv/bin/python` (POSIX) |

## 用法

在 MATRag 仓库根目录下运行 (需要 `matrag` 可 import, 即已 `pip install -e .`):

```bash
python -m tools.contract_sync.check_vocab
python -m tools.contract_sync.check_contract_hash
```

也可显式指定路径:

```bash
python -m tools.contract_sync.check_vocab --ate-root /path/to/ATEStudio
python -m tools.contract_sync.check_contract_hash --ate-root /path/to/ATEStudio --ate-python /path/to/python
```

`ATE_STUDIO_ROOT` 指向兄弟目录 (默认布局 `F:\Workspace\{MATRag,ATEStudio}`) 时
无需任何参数。

## 退出码语义

| 码 | 含义 | CI 行为 |
|---|---|---|
| `0` | 全部硬门禁通过 (仅有 WARN/INFO) | 绿灯 |
| `1` | 硬门禁漂移 (`check_vocab` #1 kind / #3 action 差集非空; 指纹不一致) | 红灯 (gate) |
| `2` | 环境/数据源错误 (路径不存在、解析失败、子进程失败等) | 红灯 (gate) |

任一非零退出码都是失败; `2` 与 `1` 区分在于**漂移**与**环境问题**, 便于排查。
无论哪种失败都**不静默降级** —— 错误信息会写明缺失的文件/条目与修复提示。

## 依赖

- 运行 `check_vocab.py` 需要 `pyyaml` (MATRag 依赖已含)。
- 运行 `check_contract_hash.py` 额外需要 ATEStudio 侧可 import `ate_cloud` 的解释器。
- 两者均**离线**可跑, 不发起任何网络/LLM 调用。
