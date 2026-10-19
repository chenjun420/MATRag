#!/bin/bash
# MATRag 板卡应用层部署（Debian 12 ARM64 / 192.168.5.25）
#
# 前置:
#   * uv 已装在 /usr/local/bin/uv（旧部署遗留，保留复用）
#   * /opt/python 有 uv 预编译 CPython 3.13（旧部署遗留，保留复用）
#   * 本仓库已解包到 /opt/matrag（含 data/ 与 .env）
#
# 用法: sudo bash /opt/matrag/deploy/native/06-install-matrag.sh
# 环境: MATRAG_UV_INDEX 可选 —— 指定 PyPI 镜像（如清华源）覆盖默认 pypi.org
set -euxo pipefail

APP_DIR=/opt/matrag
LOG_DIR=/var/log/matrag
SVC_USER=matrag
PY_HOME=/opt/python          # uv 预编译解释器（共享可读）
UV_BIN=/usr/local/bin/uv     # 系统级安装: 服务账号 matrag 必须能执行
UV_CACHE=/opt/uv-cache       # 共享缓存: jieba 无 aarch64 wheel, 源码构建只发生一次
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "===== STEP1 服务账号与目录 ====="
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -m -d "$APP_DIR" -s /bin/bash "$SVC_USER"
mkdir -p "$APP_DIR" "$LOG_DIR" "$UV_CACHE"
chmod 1777 "$UV_CACHE"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$LOG_DIR"
[ -x "$UV_BIN" ] || { echo "FATAL: uv 不存在: $UV_BIN"; exit 1; }
[ -x "$PY_HOME/cpython-3.13/bin/python3.13" ] || ls -d "$PY_HOME"/cpython-3.13*/ >/dev/null 2>&1 \
  || { echo "FATAL: /opt/python 下无 CPython 3.13"; exit 1; }

echo "===== STEP2 依赖安装（uv sync --locked; venv 自动建为 CPython 3.13） ====="
# 续行变量: 镜像可选覆盖
UV_INDEX_ARGS=()
if [ -n "${MATRAG_UV_INDEX:-}" ]; then
  UV_INDEX_ARGS=(--default-index "$MATRAG_UV_INDEX")
fi
sudo -u "$SVC_USER" env UV_PYTHON_INSTALL_DIR="$PY_HOME" UV_CACHE_DIR="$UV_CACHE" \
  "$UV_BIN" --no-config sync --locked --extra dev --python 3.13 "${UV_INDEX_ARGS[@]}" --directory "$APP_DIR"

echo "===== STEP3 导入自检 ====="
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/python" - <<'PYCHECK'
import importlib
mods = ("matrag", "semantica", "mcp", "jieba", "sympy", "pydantic_ai", "pymupdf", "fitz")
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append(f"{m}: {type(exc).__name__}: {exc}")
if bad:
    raise SystemExit("依赖导入失败:\n  " + "\n  ".join(bad))
print("依赖导入 OK:", ", ".join(mods))
PYCHECK

echo "===== STEP4 systemd ====="
cp "$SCRIPT_DIR/matrag-gateway.service" /etc/systemd/system/matrag-gateway.service
systemctl daemon-reload
systemctl enable matrag-gateway.service
systemctl restart matrag-gateway.service
sleep 6
systemctl is-active matrag-gateway.service

echo "===== STEP5 健康检查 ====="
ss -ltn | grep ':8321' || echo "WARN: 8321 未监听"
tail -20 "$LOG_DIR/gateway.err" 2>/dev/null || true
echo "== STEP6 DONE =="
