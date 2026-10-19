#!/usr/bin/env bash
# 旧栈清理（默认 dry-run）。每个动作都是显式 flag，未给 flag 只打印计划。
#
# 纪律:
#   * 必须先跑 --backup-only 并成功，才允许 --stop-services / --drop-db / --remove-dirs
#   * 删数据必须 MATRAG_CLEANUP_CONFIRM=yes（防误触）
#   * 只处理清单登记的资产（单元名/库名/目录），清单外资产先盘点再决策
#   * 板卡侧 systemctl/psql(pg_dump)/rm 需 root：统一 sudo -n 前缀（rpdzkj 免密）
#
# 2026-10-16 范围修订（用户裁定）: .25 板卡全栈旧部署全部移除 —— ATERag(mcp+explorer)
# 与 ATEStudio(ate-cloud + nats) 及 /opt 下历史残留 aterag.* 目录一并清理；
# ATEStudio 后续再对接新 MATRag。所有资产先备份后删除，保留 PostgreSQL 服务本体、
# uv 与 /opt/python（新 MATRag 部署复用）。
#
# 用法:
#   bash scripts/deploy_cleanup.sh --hosts aterag-board                             # 只打印计划
#   bash scripts/deploy_cleanup.sh --hosts ... --backup-only                        # 备份
#   MATRAG_CLEANUP_CONFIRM=yes \
#     bash scripts/deploy_cleanup.sh --hosts ... --stop-services                    # 停服
#   MATRAG_CLEANUP_CONFIRM=yes \
#     bash scripts/deploy_cleanup.sh --hosts ... --drop-db --remove-dirs --remove-units --remove-users  # 删除
#
# WSL 注意: 默认备份目录取本仓库 data/backups（$(dirname $0)/..），WSL/GitBash 下
# 均落回真实挂载路径；如需指定用 MATRAG_CLEANUP_BACKUP_DIR（WSL 传 /mnt/f/...）。
set -euo pipefail

BACKUP_ONLY=0 STOP=0 DROP_DB=0 RM_DIRS=0 RM_UNITS=0 RM_USERS=0 DRY=1
HOSTS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --hosts) HOSTS="$2"; shift 2;;
    --backup-only) BACKUP_ONLY=1; DRY=0; shift;;
    --stop-services) STOP=1; DRY=0; shift;;
    --drop-db) DROP_DB=1; DRY=0; shift;;
    --remove-dirs) RM_DIRS=1; DRY=0; shift;;
    --remove-units) RM_UNITS=1; DRY=0; shift;;
    --remove-users) RM_USERS=1; DRY=0; shift;;
    -h|--help) sed -n '2,27p' "$0"; exit 0;;
    *) echo "unknown flag: $1"; exit 2;;
  esac
done

[ -n "$HOSTS" ] || { echo "--hosts 必填"; exit 2; }
IFS=',' read -r -a HOST_ARR <<< "$HOSTS"
BACKUP_DIR="${MATRAG_CLEANUP_BACKUP_DIR:-$(cd "$(dirname "$0")/.." && pwd)/data/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET_BACKUP="$BACKUP_DIR/oldstack-$STAMP"

# 登记清单（2026-10-16 盘点确认）
UNITS=(aterag-mcp aterag-http aterag-explorer ate-cloud nats)   # aterag-http 不存在，skip 无害
DB_NAMES=(power_specs w1probe ate_studio)                       # w1probe=ATERag probe 库; ate_studio=ATEStudio
DIRS=(/opt/aterag /opt/aterag_backups /opt/aterag-bundle /opt/aterag-drill /opt/aterag-drill-pkg \
      /opt/aterag.new /opt/aterag.old /opt/aterag.prev \
      /opt/atestudio /opt/nats /var/lib/nats /var/log/aterag /var/log/nats)
FILES=(/usr/local/bin/nats-server)
USERS=(aterag atestudio nats)
ENV_FILES=(/opt/aterag/.env /opt/atestudio/.env /opt/atestudio/.env.local)

say() { printf '%s\n' "$*"; }

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)
[ -n "${MATRAG_BOARD_SSH_USER:-}" ] && SSH_OPTS+=(-l "$MATRAG_BOARD_SSH_USER")
[ -n "${MATRAG_BOARD_SSH_KEY:-}" ] && SSH_OPTS+=(-i "$MATRAG_BOARD_SSH_KEY")

run_remote() { # host, cmd...
  local h="$1"; shift
  ssh "${SSH_OPTS[@]}" "$h" "$*"
}

# ---------- dry-run 计划 ----------
if [ "$DRY" -eq 1 ]; then
  say "===== 旧栈清理计划（DRY-RUN，不执行）====="
  say "主机: ${HOST_ARR[*]}"
  say "systemd 单元: ${UNITS[*]}"
  say "PG 库: ${DB_NAMES[*]}"
  say "目录: ${DIRS[*]}"
  say "散件: ${FILES[*]}"
  say "服务账号(可选): ${USERS[*]}"
  say "备份目录: $TARGET_BACKUP"
  say "动作: (无 —— 未给动作 flag)"
  say "顺序: --backup-only → --stop-services → --drop-db --remove-dirs --remove-units [--remove-users]"
  exit 0
fi

# ---------- 备份 ----------
if [ "$BACKUP_ONLY" -eq 1 ]; then
  mkdir -p "$TARGET_BACKUP"
  for h in "${HOST_ARR[@]}"; do
    say "== backup @ $h -> $TARGET_BACKUP =="
    # 0) 现场快照（清单留档）
    {
      echo "# 盘点快照 $(date -Is)"
      run_remote "$h" "hostname; uname -m; ls -la /opt; ss -ltn; systemctl list-unit-files --no-pager | grep -iE 'aterag|atestudio|ate-cloud|nats' || true; sudo -n -u postgres psql -lt"
    } > "$TARGET_BACKUP/$h.snapshot.txt" 2>&1 || true
    # 1) 每个库一份 pg_dump（root 走 peer 认证为 postgres）
    for db in "${DB_NAMES[@]}"; do
      run_remote "$h" "sudo -n -u postgres pg_dump -Fc '$db'" \
        > "$TARGET_BACKUP/$h.$db.pg_dump" 2> "$TARGET_BACKUP/$h.$db.pg.err" || {
        say "[FAIL] pg_dump $db @ $h（详见 $TARGET_BACKUP/$h.$db.pg.err）"; exit 1; }
    done
    # 2) ATERag 目录族（含历史残留）
    run_remote "$h" "sudo -n tar czf - /opt/aterag /opt/aterag_backups /opt/aterag-bundle /opt/aterag-drill /opt/aterag-drill-pkg /opt/aterag.new /opt/aterag.old /opt/aterag.prev /var/log/aterag 2>/dev/null" \
      > "$TARGET_BACKUP/$h.aterag.tgz" || true
    # 3) ATEStudio + NATS 目录与散件
    run_remote "$h" "sudo -n tar czf - /opt/atestudio /opt/nats /var/lib/nats /var/log/nats /usr/local/bin/nats-server 2>/dev/null" \
      > "$TARGET_BACKUP/$h.atestudio-nats.tgz" || true
    # 4) 单元文件原文
    for u in "${UNITS[@]}"; do
      run_remote "$h" "sudo -n cat /etc/systemd/system/$u.service 2>/dev/null" \
        > "$TARGET_BACKUP/$h.$u.unit" || true
    done
    # 5) .env（含密钥，仅落本地备份）
    for f in "${ENV_FILES[@]}"; do
      base="$(basename "$f")"
      run_remote "$h" "sudo -n cat $f 2>/dev/null" > "$TARGET_BACKUP/$h.$(echo "$f" | tr '/' '_').bak" || true
      : "$base"
    done
    # 6) 定时任务（ssh 用户 + 各服务账号）
    run_remote "$h" "crontab -l 2>/dev/null" > "$TARGET_BACKUP/$h.crontab" || true
    for u in "${USERS[@]}"; do
      run_remote "$h" "sudo -n crontab -l -u $u 2>/dev/null" > "$TARGET_BACKUP/$h.crontab.$u" || true
    done
  done
  # 校验: pg_dump 文件非空且头是 pg 自定义格式
  for f in "$TARGET_BACKUP"/*.pg_dump; do
    [ -s "$f" ] || { say "[FAIL] 备份为空: $f（空备份不允许后续清理）"; exit 1; }
    head -c 5 "$f" | grep -q "PGDMP" || { say "[WARN] $f 不是 PGDMP 头，请人工确认"; }
  done
  # 校验: tar 非空
  for f in "$TARGET_BACKUP"/*.tgz; do
    [ -s "$f" ] || { say "[WARN] tar 备份为空: $f"; }
  done
  say "[OK] 备份完成: $TARGET_BACKUP"; exit 0
fi

# ---------- 破坏性动作（需确认） ----------
[ "${MATRAG_CLEANUP_CONFIRM:-}" = "yes" ] || {
  echo "[FAIL] 破坏性动作需要 MATRAG_CLEANUP_CONFIRM=yes（且应已完成 --backup-only）"; exit 1; }
[ -d "${TARGET_BACKUP%/*}" ] || say "[WARN] $TARGET_BACKUP 的父目录存在性未知，确认备份已另行保管"

for h in "${HOST_ARR[@]}"; do
  say "== cleanup @ $h =="
  if [ "$STOP" -eq 1 ]; then
    for u in "${UNITS[@]}"; do
      run_remote "$h" "sudo -n systemctl stop $u 2>/dev/null && sudo -n systemctl disable $u 2>/dev/null" \
        && say "  stopped+disabled: $u" || say "  skip(not active): $u"
    done
  fi
  if [ "$DROP_DB" -eq 1 ]; then
    for db in "${DB_NAMES[@]}"; do
      run_remote "$h" "sudo -n -u postgres psql -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$db';\" -c 'DROP DATABASE IF EXISTS \"$db\";'" \
        && say "  dropped db: $db" || say "  [FAIL] drop db $db @ $h"
    done
  fi
  if [ "$RM_DIRS" -eq 1 ]; then
    for d in "${DIRS[@]}"; do
      run_remote "$h" "sudo -n rm -rf $d" && say "  removed: $d" || say "  [FAIL] rm: $d"
    done
    for f in "${FILES[@]}"; do
      run_remote "$h" "sudo -n rm -f $f" && say "  removed file: $f" || true
    done
  fi
  if [ "$RM_UNITS" -eq 1 ]; then
    for u in "${UNITS[@]}"; do
      run_remote "$h" "sudo -n rm -f /etc/systemd/system/$u.service && sudo -n systemctl daemon-reload" \
        && say "  unit file removed: $u" || true
    done
  fi
  if [ "$RM_USERS" -eq 1 ]; then
    for u in "${USERS[@]}"; do
      run_remote "$h" "sudo -n userdel $u 2>/dev/null" && say "  user removed: $u" || say "  [SKIP] userdel $u"
    done
  fi
done
say "[done] 现在重跑 scripts/deploy_inventory.sh 验证端口/库/进程已释放，并贴入部署记录。"
