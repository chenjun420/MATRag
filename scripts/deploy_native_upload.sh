#!/usr/bin/env bash
# MATRag: 打包代码+数据并上传板卡（在仓库根以 WSL bash 执行）
# 用法: bash scripts/deploy_native_upload.sh [host]   # host 默认 aterag-board
set -euo pipefail
HOST="${1:-aterag-board}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
CODE_TGZ=/tmp/matrag-code.tgz
DATA_TGZ=/tmp/matrag-data.tgz

echo "== 打包代码（pyproject/uv.lock/src/scripts/deploy/文档） =="
tar czf "$CODE_TGZ" \
  --exclude='__pycache__' --exclude='*.egg-info' --exclude='.pytest_cache' \
  pyproject.toml uv.lock src scripts deploy README.md TODO.md AGENTS.md .env.example docs

echo "== 打包数据（store/corpus/domain/reports） =="
tar czf "$DATA_TGZ" -C "$REPO" data/store data/corpus data/domain data/reports

ls -l "$CODE_TGZ" "$DATA_TGZ"
echo "== 上传到 $HOST:/tmp/ =="
scp "$CODE_TGZ" "$DATA_TGZ" "$HOST:/tmp/"
echo "== 上传完成（板卡侧解包见 deploy/native/README.md） =="
