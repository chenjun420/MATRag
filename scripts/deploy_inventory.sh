#!/usr/bin/env bash
# ATERag 资产盘点（只读）。产出: 单元/端口/进程/目录/crontab 清单。
# 用法（PowerShell 或 bash 均可）:
#   MATRAG_BOARD_HOST=192.168.5.25 bash scripts/deploy_inventory.sh 192.168.5.25 ...
set -euo pipefail

HOSTS=("$@")
if [ ${#HOSTS[@]} -eq 0 ]; then
  [ -n "${MATRAG_BOARD_HOST:-}" ] && HOSTS=("$MATRAG_BOARD_HOST") || {
    echo "usage: $0 <host> [host...]  (或设 MATRAG_BOARD_HOST)"; exit 2; }
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)
[ -n "${MATRAG_BOARD_SSH_USER:-}" ] && SSH_OPTS+=(-l "$MATRAG_BOARD_SSH_USER")
[ -n "${MATRAG_BOARD_SSH_KEY:-}" ] && SSH_OPTS+=(-i "$MATRAG_BOARD_SSH_KEY")

REMOTE_CMDS='
echo "== units =="
systemctl list-units --all --no-pager 2>/dev/null | grep -i aterag || echo "(no aterag units)"
echo "== ports (8080/8090/8000/5432) =="
ss -ltnp 2>/dev/null | grep -E ":(8080|8090|8000|5432)[[:space:]]" || echo "(no listeners on those ports)"
echo "== processes =="
ps aux | grep -i "[a]terag" || echo "(no aterag processes)"
echo "== dirs =="
ls -ld /opt/aterag /var/log/aterag 2>/dev/null || echo "(no /opt/aterag or /var/log/aterag)"
echo "== unit files =="
systemctl cat aterag-mcp 2>/dev/null | head -5 || true
ls /etc/systemd/system/ 2>/dev/null | grep -i aterag || echo "(no unit files)"
echo "== postgres databases =="
psql -U postgres -lt 2>/dev/null | grep -E "power_specs|matrag" || echo "(cannot list or no aterag db; try -U <owner>)"
echo "== crontab =="
crontab -l 2>/dev/null | grep -i aterag || echo "(no crontab entries)"
echo "== ate-cloud health (同机 ATEStudio, 不应受清理影响) =="
curl -s -o /dev/null -w "ate-cloud /health: %{http_code}\n" http://127.0.0.1:8000/health || true
'

rc=0
for h in "${HOSTS[@]}"; do
  echo "================ $h ================"
  if ssh "${SSH_OPTS[@]}" "$h" "$REMOTE_CMDS"; then :; else
    echo "[WARN] inventory failed on $h"; rc=1
  fi
done
exit $rc
