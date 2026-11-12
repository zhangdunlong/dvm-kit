#!/usr/bin/env bash
# ============================================================
# DVM Kit · IPv6 地址守护
#
# 职责：
#   1. 保证 IPv6 协议栈与 accept_ra 开启
#   2. 检测公网 IPv6 变化，写入文件供 DDNS 读取
#   3. 可选：保活 Caddy
#
# 用法： setsid nohup bash scripts/ipv6-guard.sh >> .logs/guard.log 2>&1 &
# 环境变量：
#   GUARD_IFACE      网卡名，默认自动探测
#   GUARD_INTERVAL   检查间隔秒数，默认 60
#   GUARD_KEEP_CADDY 设为 1 时保活 caddy
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.dvmkit"
OUT="$STATE/ipv6-current.txt"
LOG="$ROOT/.logs/guard.log"
INTERVAL="${GUARD_INTERVAL:-60}"
KEEP_CADDY="${GUARD_KEEP_CADDY:-0}"

mkdir -p "$STATE" "$(dirname "$LOG")"

pick_iface() {
  if [ -n "${GUARD_IFACE:-}" ]; then echo "$GUARD_IFACE"; return; fi
  for i in $(ls /sys/class/net 2>/dev/null); do
    [ "$i" = "lo" ] && continue
    [ -d "/sys/class/net/$i" ] || continue
    case "$i" in
      docker*|veth*|br-*|tun*|tap*|zt*|wg*) continue ;;
    esac
    if ip -6 addr show "$i" 2>/dev/null | grep -q "scope global"; then
      echo "$i"; return
    fi
  done
  echo ""
}

IFACE="$(pick_iface)"
echo "[$(date '+%F %T')] guard start iface=${IFACE:-auto} interval=${INTERVAL}s" >> "$LOG"

# 取公网地址：
#   1) 优先 stable-privacy（服务实际监听在这个地址上，DDNS 要登记它）
#   2) 严格排除 fe80(链路本地) 与 fd(ULA)，否则会把内网地址写进公网 DNS
get_v6() {
  local i="$1"
  ip -6 addr show "$i" 2>/dev/null \
    | grep 'scope global' \
    | grep 'stable-privacy' \
    | grep -oE 'inet6 [0-9a-f:]+' | awk '{print $2}' \
    | grep -vE '^(fe80|fd)' | head -1
}

get_v6_any() {
  local i="$1"
  ip -6 addr show "$i" 2>/dev/null \
    | grep 'scope global' \
    | grep -oE 'inet6 [0-9a-f:]+' | awk '{print $2}' \
    | grep -vE '^(fe80|fd)' | head -1
}

LAST=""
while true; do
  [ -z "$IFACE" ] && IFACE="$(pick_iface)"

  if [ -n "$IFACE" ]; then
    # ---- 保证协议栈开启 ----
    for k in /proc/sys/net/ipv6/conf/all/disable_ipv6 \
             /proc/sys/net/ipv6/conf/default/disable_ipv6; do
      [ -f "$k" ] && [ "$(cat "$k" 2>/dev/null)" != "0" ] && echo 0 > "$k" 2>/dev/null
    done
    ra="/proc/sys/net/ipv6/conf/$IFACE/accept_ra"
    [ -f "$ra" ] && [ "$(cat "$ra" 2>/dev/null)" != "1" ] && echo 1 > "$ra" 2>/dev/null

    V6="$(get_v6 "$IFACE")"
    [ -z "$V6" ] && V6="$(get_v6_any "$IFACE")"

    if [ -n "$V6" ] && [ "$V6" != "$LAST" ]; then
      printf '%s\n' "$V6" > "$OUT.tmp" && mv -f "$OUT.tmp" "$OUT"
      LAST="$V6"
      echo "[$(date '+%F %T')] ipv6 -> $V6" >> "$LOG"
    fi
  fi

  if [ "$KEEP_CADDY" = "1" ] && ! pgrep -f "[c]addy run" >/dev/null 2>&1; then
    if command -v caddy >/dev/null 2>&1 && [ -f "$ROOT/scripts/Caddyfile" ]; then
      ( cd "$ROOT/scripts" && setsid nohup caddy run --config "$ROOT/scripts/Caddyfile" \
          --adapter caddyfile >> "$ROOT/.logs/caddy.log" 2>&1 < /dev/null & )
      echo "[$(date '+%F %T')] caddy restarted" >> "$LOG"
    fi
  fi

  sleep "$INTERVAL"
done
