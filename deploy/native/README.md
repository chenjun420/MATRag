# MATRag 板卡部署（native，192.168.5.25）

旧部署（ATERag + ATEStudio/nats）已于 2026-10-16 全量清理；本目录是 MATRag 的
全新部署资产，部署形态与旧栈保持同构（uv + /opt/python CPython 3.13 + systemd）。

## 组件

| 文件 | 作用 |
|---|---|
| `06-install-matrag.sh` | 应用层安装：服务账号/目录 → `uv sync --locked` → 导入自检 → systemd 起服 |
| `matrag-gateway.service` | MCP over streamable-http 常驻单元（端口取 `.env` 的 `MATRAG_GATEWAY_PORT`，默认 8321） |
| `07-install-explorer.sh` | Explorer 安装：隔离依赖就位 → 生成/保持 `SEMANTICA_API_KEY` → systemd 起服 |
| `matrag-explorer.service` | Knowledge Explorer 只读可视化单元（`0.0.0.0:8899`，`X-API-Key` 鉴权） |

## 部署步骤（在开发机侧, WSL bash 执行）

```bash
# 1. 打包上传（代码 + data/store|corpus|domain|reports）
bash scripts/deploy_native_upload.sh          # 见仓库 scripts/ 同名脚本
# 2. 板卡侧解包 + 服务账号/目录
ssh aterag-board bash -s < <(解包脚本)         # 见部署记录
# 3. 安装（自动建 venv / 装依赖 / 起服）
ssh aterag-board "sudo -n bash /opt/matrag/deploy/native/06-install-matrag.sh"
# 4. 验证（本地巡检 + 远端 MCP 客户端）
bash scripts/deploy_inventory.sh aterag-board
```

## Explorer（可选组件）

semantica Knowledge Explorer（只读知识图谱可视化，`0.0.0.0:8899`，`X-API-Key` 鉴权，
与 gateway 同构：`matrag` 用户 + `.env` 600 + 日志落 `/var/log/matrag`）：

```bash
# 板卡侧（依赖子集从开发机预置: scp board-deps.tgz 到 /tmp）
sudo bash /opt/matrag/deploy/native/07-install-explorer.sh
# 访问: http://192.168.5.25:8899/  请求头 X-API-Key: /opt/matrag/.env 的 SEMANTICA_API_KEY
```

依赖隔离在 `/opt/matrag/.tools/explorer-deps`（fastapi + starlette + annotated_doc，
纯 Python 包），不污染主 venv；`.tools/explorer-deps` 缺失时 `scripts/explorer_serve.py`
首次启动会自动 `pip install --target` 补齐。

## 约定

- 单点真相是仓库的 `pyproject.toml` + `uv.lock`（不在脚本里硬编码包列表）。
- `jieba` 无 aarch64 wheel，首次 `uv sync` 从 sdist 构建；`/opt/uv-cache`（1777）
  共享缓存保证只构建一次。
- `.env` 权限 600 属主 `matrag`，含 LLM/Embedding Key 与 gateway API key（fail-closed，
  未配 key 拒绝启动）。
- 当前为 dev profile（sqlite 索引随 `data/store` 分发）；PG(pgvector/age) 后端接线
  见 TODO P1-8，板卡 PostgreSQL 17 服务本体保留待用。
