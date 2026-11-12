#!/usr/bin/env bash
# ============================================================
# DVM Kit · 启动全部服务
# 用法： bash scripts/start.sh [console|dashboard|caddy|all]
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.dvmkit"
LOG="$ROOT/.logs"
ENVF="$STATE/dvmkit.env"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }
info() { echo -e "  ${DIM}·${NC} $*"; }

[ -f "$ENVF" ] || { bad "缺少 $ENVF，请先运行 bash scripts/install.sh"; exit 1; }
set -a; . "$ENVF"; set +a
mkdir -p "$LOG" "$STATE/pids"

TARGET="${1:-all}"

is_up() {   # is_up <port>
  (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$1 ") \
    || (command -v netstat >/dev/null 2>&1 && netstat -tln 2>/dev/null | grep -q ":$1 ")
}

wait_port() {  # wait_port <port> <seconds>
  local i=0
  while [ $i -lt "$2" ]; do
    is_up "$1" && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

start_proc() {  # start_proc <name> <port> <workdir> <cmd...>
  local name="$1" port="$2" wd="$3"; shift 3
  local pidf="$STATE/pids/$name.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    ok "$name 已在运行 (PID $(cat "$pidf"))"; return 0
  fi
  if is_up "$port"; then
    warn "$name 端口 $port 已被占用，跳过"; return 0
  fi
  ( cd "$wd" && setsid nohup "$@" >> "$LOG/$name.log" 2>&1 < /dev/null & echo $! > "$pidf" )
  if wait_port "$port" 20; then
    ok "$name 已启动 :$port (PID $(cat "$pidf" 2>/dev/null))"
  else
    bad "$name 启动失败，最后 15 行日志："
    tail -15 "$LOG/$name.log" 2>/dev/null | sed 's/^/      /'
    return 1
  fi
}

echo
echo "=============== 启动 DVM Kit ==============="
echo

# ---------- 更新中心 ----------
if [ "$TARGET" = "all" ] || [ "$TARGET" = "console" ]; then
  echo "[console] 项目更新中心"
  if [ ! -f "$ROOT/console/token" ]; then
    python3 - "$ROOT/console/token" <<'PY'
import base64, os, sys
p = sys.argv[1]
with open(p, "w") as f:
    f.write(base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=") + "\n")
os.chmod(p, 0o600)
PY
    ok "已生成访问令牌：$ROOT/console/token"
  fi
  start_proc console "$PANEL_PORT" "$ROOT/console" \
    python3 "$ROOT/console/ag2a-console.py" || true
  echo
fi

# ---------- 仪表盘 ----------
if [ "$TARGET" = "all" ] || [ "$TARGET" = "dashboard" ]; then
  echo "[dashboard] 状态仪表盘"
  start_proc dashboard "$DASH_PORT" "$ROOT/dashboard" \
    python3 "$ROOT/dashboard/dvm-dashboard.py" || true
  echo
fi

# ---------- Caddy ----------
if [ "$TARGET" = "all" ] || [ "$TARGET" = "caddy" ]; then
  echo "[caddy] HTTPS 入口"
  if ! command -v caddy >/dev/null 2>&1; then
    warn "未安装 caddy，跳过（仅内网访问可不需要）"
  elif [ ! -f "$ROOT/scripts/Caddyfile" ]; then
    warn "缺少 scripts/Caddyfile，跳过"
  else
    if [ -f "$STATE/pids/caddy.pid" ] && kill -0 "$(cat "$STATE/pids/caddy.pid" 2>/dev/null)" 2>/dev/null; then
      ok "caddy 已在运行"
    else
      ( cd "$ROOT/scripts" && \
        setsid nohup caddy run --config "$ROOT/scripts/Caddyfile" --adapter caddyfile \
          >> "$LOG/caddy.log" 2>&1 < /dev/null & echo $! > "$STATE/pids/caddy.pid" )
      sleep 4
      if is_up 443; then ok "caddy 已启动 :443"; else
        bad "caddy 未监听 443，日志："
        tail -15 "$LOG/caddy.log" 2>/dev/null | sed 's/^/      /'
      fi
    fi
  fi
  echo
fi

echo "---------------------------------------------"
[ -n "${PANEL_PORT:-}" ] && info "更新中心  http://127.0.0.1:$PANEL_PORT/ag2a/  （令牌见 console/token）"
[ -n "${DASH_PORT:-}" ]  && info "仪表盘    http://127.0.0.1:$DASH_PORT/"
info "停止服务： bash scripts/stop.sh"
echo
