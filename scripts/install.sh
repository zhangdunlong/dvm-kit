#!/usr/bin/env bash
# ============================================================
# DVM Kit · 环境检测与初始化
# 只做检测和目录准备，不擅自安装系统包、不修改防火墙。
# 用法： bash scripts/install.sh
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.dvmkit"
GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
bad()  { echo -e "  ${RED}✗${NC} $*"; }
info() { echo -e "  ${DIM}·${NC} $*"; }

echo
echo "=============== DVM Kit 环境检测 ==============="
echo

# ---------- 1. 运行环境 ----------
echo "[1/6] 运行环境"
UNAME_M="$(uname -m 2>/dev/null || echo unknown)"
case "$UNAME_M" in
  aarch64|arm64|armv8*) ARCH=arm64 ;;
  armv7l|armhf)         ARCH=armv7 ;;
  x86_64|amd64)         ARCH=amd64 ;;
  *)                    ARCH=unknown ;;
esac
ok "架构：$UNAME_M -> $ARCH"

ENV_KIND="linux"
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then
  ENV_KIND="termux"
elif [ -f /system/build.prop ] && [ -x /system/bin/app_process ]; then
  ENV_KIND="android-chroot"
elif [ -f /etc/os-release ] && grep -qi debian /etc/os-release 2>/dev/null; then
  ENV_KIND="debian"
fi
ok "环境：$ENV_KIND"

if [ "$ENV_KIND" = "termux" ]; then
  info "Termux 下建议先执行： pkg install python curl openssl-tool"
elif [ "$ENV_KIND" = "android-chroot" ]; then
  info "chroot 内需保证 /proc /sys /dev 已挂载，否则服务会异常"
fi

# ---------- 2. 必备命令 ----------
echo
echo "[2/6] 依赖命令"
need_py=0
for c in python3 curl; do
  if command -v "$c" >/dev/null 2>&1; then
    ok "$c: $($c --version 2>&1 | head -1)"
  else
    bad "$c 缺失"
    [ "$c" = python3 ] && need_py=1
  fi
done
if command -v caddy >/dev/null 2>&1; then
  ok "caddy: $(caddy version 2>&1 | head -1)"
else
  warn "caddy 未安装（走 Tunnel 方案时可不需要）"
fi
if command -v cloudflared >/dev/null 2>&1; then
  ok "cloudflared: $(cloudflared --version 2>&1 | head -1)"
else
  warn "cloudflared 未安装（走 IPv6 方案时可不需要）"
fi

# ---------- 3. IPv6 能力 ----------
echo
echo "[3/6] 公网 IPv6"
V6=""
for iface in $(ls /sys/class/net 2>/dev/null); do
  [ "$iface" = "lo" ] && continue
  cand=$(ip -6 addr show "$iface" 2>/dev/null \
    | grep 'scope global' | grep -oE 'inet6 [0-9a-f:]+' | awk '{print $2}' \
    | grep -vE '^(fe80|fd)' | head -1)
  [ -n "$cand" ] && V6="$cand" && break
done
if [ -n "$V6" ]; then
  ok "公网 IPv6：$V6"
  info "→ 可以走 A 方案（IPv6 直连）"
else
  warn "未检测到公网 IPv6"
  info "→ 建议走 B 方案（Cloudflare Tunnel）"
fi

# ---------- 4. 端口占用 ----------
echo
echo "[4/6] 端口占用"
for p in 80 443 3065 3077 5700 8765; do
  if (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$p ") \
     || (command -v netstat >/dev/null 2>&1 && netstat -tln 2>/dev/null | grep -q ":$p "); then
    warn "$p 已被占用"
  else
    info "$p 空闲"
  fi
done

# ---------- 5. 目录准备 ----------
echo
echo "[5/6] 初始化目录"
mkdir -p "$STATE" "$ROOT/.logs"
ok "状态目录：$STATE"
ok "日志目录：$ROOT/.logs"

if [ ! -f "$ROOT/scripts/Caddyfile" ]; then
  cp "$ROOT/scripts/Caddyfile.example" "$ROOT/scripts/Caddyfile"
  ok "已生成 Caddyfile（请修改其中的 example.com）"
else
  info "Caddyfile 已存在，跳过"
fi

if [ ! -f "$STATE/dvmkit.env" ]; then
  cat > "$STATE/dvmkit.env" <<'EOF'
# DVM Kit 运行配置（不要提交到 Git）
# 磁盘上仅本机可读
PANEL_PORT=3077
DASH_PORT=8765
QL_PORT=5700
API_PORT=3065

# 你的域名（走 Caddy 时使用）
DOMAIN_PANEL=dash.example.com
DOMAIN_QL=ql.example.com
DOMAIN_API=api.example.com

# Let's Encrypt 通知邮箱
ACME_EMAIL=you@example.com

# 仅 DNS-01 方案需要（Zone.DNS.Edit 权限）
CF_API_TOKEN=
EOF
  chmod 600 "$STATE/dvmkit.env"
  ok "已生成 $STATE/dvmkit.env（请填写域名与令牌）"
else
  info "配置文件已存在，跳过"
fi

# ---------- 6. 下一步 ----------
echo
echo "[6/6] 下一步"
echo "  1) 编辑 $STATE/dvmkit.env 与 scripts/Caddyfile"
echo "  2) bash scripts/start.sh 启动服务"
echo "  3) 打开 http://<手机IP>:8765/ 看仪表盘"
echo
if [ "$need_py" = "1" ]; then
  bad "缺少 python3，面板与仪表盘无法运行"
  exit 1
fi
echo "检测完成。"
