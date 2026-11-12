#!/usr/bin/env bash
# ============================================================
# DVM Kit · 停止全部服务
# 用法： bash scripts/stop.sh [console|dashboard|caddy|all]
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.dvmkit"
PIDDIR="$STATE/pids"
GREEN='\033[0;32m'; YELLOW='\033[0;33m'; DIM='\033[2m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
info() { echo -e "  ${DIM}·${NC} $*"; }

TARGET="${1:-all}"

stop_one() {  # stop_one <name>
  local name="$1" pidf="$PIDDIR/$1.pid"
  if [ ! -f "$pidf" ]; then
    info "$name 无 PID 文件，跳过"; return 0
  fi
  local pid; pid="$(cat "$pidf" 2>/dev/null)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    warn "$name 进程已不存在，清理 PID 文件"; rm -f "$pidf"; return 0
  fi
  kill -TERM "$pid" 2>/dev/null
  local i=0
  while [ $i -lt 15 ] && kill -0 "$pid" 2>/dev/null; do sleep 1; i=$((i+1)); done
  if kill -0 "$pid" 2>/dev/null; then
    warn "$name 未响应 TERM，强制结束"
    kill -KILL "$pid" 2>/dev/null
  fi
  rm -f "$pidf"
  ok "$name 已停止"
}

echo
echo "=============== 停止 DVM Kit ==============="
echo

case "$TARGET" in
  all)
    stop_one console
    stop_one dashboard
    stop_one caddy
    ;;
  console|dashboard|caddy) stop_one "$TARGET" ;;
  *) echo "未知目标：$TARGET（可选 console|dashboard|caddy|all）"; exit 1 ;;
esac
echo
