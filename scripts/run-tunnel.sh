#!/usr/bin/env bash
# ============================================================
# DVM Kit · Cloudflare Tunnel 启动器
# 用法： bash scripts/run-tunnel.sh [quic|http2]
# 环境变量：
#   CLOUDFLARED_CONFIG  默认 ~/.cloudflared/config.yml
#   TUNNEL_EDGE_IPS     可选，逗号分隔的固定 edge，如 "198.41.192.57:7844,198.41.200.233:7844"
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.dvmkit"
LOG="$ROOT/.logs"
PIDFILE="$STATE/pids/cloudflared.pid"
CONF="${CLOUDFLARED_CONFIG:-$HOME/.cloudflared/config.yml}"
PROTO="${1:-${TUNNEL_PROTOCOL:-quic}}"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }
info() { echo -e "  ${DIM}·${NC} $*"; }

mkdir -p "$LOG" "$STATE/pids"

if ! command -v cloudflared >/dev/null 2>&1; then
  bad "未安装 cloudflared"
  echo "    下载： https://github.com/cloudflare/cloudflared/releases"
  exit 1
fi
if [ ! -f "$CONF" ]; then
  bad "找不到配置：$CONF"
  echo "    参考 docs/deploy-tunnel.md 生成 config.yml"
  exit 1
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  ok "cloudflared 已在运行 (PID $(cat "$PIDFILE"))"
  exit 0
fi

ARGS=(--config "$CONF" --no-autoupdate tunnel --protocol "$PROTO" run)

# 手机上的代理软件常把 DNS 劫持成 fake-ip(198.18.x.x)，
# 导致 cloudflared 解析到私有地址直接握手失败。固定 edge IP 可绕开。
if [ -n "${TUNNEL_EDGE_IPS:-}" ]; then
  IFS=',' read -ra EDGES <<< "$TUNNEL_EDGE_IPS"
  for e in "${EDGES[@]}"; do
    e="$(echo "$e" | xargs)"
    [ -n "$e" ] && ARGS+=(--edge "$e")
  done
  info "固定 edge: $TUNNEL_EDGE_IPS"
fi

info "协议：$PROTO"
setsid nohup cloudflared "${ARGS[@]}" >> "$LOG/cloudflared.log" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

sleep 6
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  ok "cloudflared 已启动 (PID $(cat "$PIDFILE"))"
  # 尝试读 metrics 确认有几条健康连接
  if command -v curl >/dev/null 2>&1; then
    n=$(curl -s --noproxy '*' --max-time 4 http://127.0.0.1:20241/metrics 2>/dev/null \
        | grep '^cloudflared_tunnel_ha_connections' | awk '{print $2}')
    [ -n "$n" ] && info "健康连接数：$n"
  fi
else
  bad "启动失败，最后 20 行日志："
  tail -20 "$LOG/cloudflared.log" | sed 's/^/      /'
  rm -f "$PIDFILE"
  exit 1
fi

echo
info "查看日志： tail -f $LOG/cloudflared.log"
info "停止：     kill \$(cat $PIDFILE)"
