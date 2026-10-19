#!/bin/bash
# MATRag Explorer 部署（semantica explore 只读可视化 / 独立进程 + X-API-Key）
#
# 前置:
#   * 06-install-matrag.sh 已完成（/opt/matrag + .venv + data）
#   * explorer 隔离依赖已就位（/opt/matrag/.tools/explorer-deps，纯 Python 子集:
#     fastapi + starlette + annotated_doc；由开发机 scp 预置或首次启动时
#     scripts/explorer_serve.py 自动 pip 安装）
#
# 用法: sudo bash /opt/matrag/deploy/native/07-install-explorer.sh
set -euxo pipefail

APP_DIR=/opt/matrag
SVC_USER=matrag
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "===== STEP1 隔离依赖检查 ====="
if [ ! -d "$APP_DIR/.tools/explorer-deps/fastapi" ]; then
  echo "WARN: $APP_DIR/.tools/explorer-deps/fastapi 不存在——首次启动时将尝试 pip 安装（需要 PyPI 网络）"
else
  echo "OK: $APP_DIR/.tools/explorer-deps 已预置"
fi

echo "===== STEP2 SEMANTICA_API_KEY（.env 无则生成） ====="
if ! grep -q '^SEMANTICA_API_KEY=' "$APP_DIR/.env"; then
  KEY="$(openssl rand -hex 24)"
  echo "SEMANTICA_API_KEY=$KEY" >> "$APP_DIR/.env"
  echo "已生成 SEMANTICA_API_KEY 并写入 $APP_DIR/.env"
else
  echo "SEMANTICA_API_KEY 已存在，保持不变"
fi
chown "$SVC_USER:$SVC_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "===== STEP3 systemd ====="
cp "$SCRIPT_DIR/matrag-explorer.service" /etc/systemd/system/matrag-explorer.service
systemctl daemon-reload
systemctl enable --now matrag-explorer.service
sleep 6
systemctl is-active matrag-explorer.service

echo "===== STEP4 健康检查 ====="
ss -ltn | grep ':8899' || echo "WARN: 8899 未监听"
KEY="$(grep '^SEMANTICA_API_KEY=' "$APP_DIR/.env" | cut -d= -f2)"
curl -s -o /dev/null -w "no-key  /api/health -> %{http_code}\n" http://127.0.0.1:8899/api/health || true
curl -s -o /dev/null -w "with-key /api/health -> %{http_code}\n" -H "X-API-Key: $KEY" http://127.0.0.1:8899/api/health || true
curl -s -o /dev/null -w "with-key /api/graph/nodes -> %{http_code}\n" -H "X-API-Key: $KEY" "http://127.0.0.1:8899/api/graph/nodes?limit=1" || true
tail -10 /var/log/matrag/explorer.err 2>/dev/null || true
echo "== DONE =="
