#!/system/bin/sh
# ============================================================
# 项目更新中心 / Project Update Hub · 部署脚本 v3.0
# 在 Android 层以 root 执行；控制台本体放进 DroidVM chroot 的 /opt。
#
#   sh install.sh [本地当前版本]     安装 + 启动 + 开机自启（幂等）
#   sh install.sh status             查看 token / pid / 探活
#   sh install.sh uninstall          移除控制台（不动业务数据）
#
# 目录（Android 层视角）：/data/local/droidvm/rootfs/opt/ag2a-console
# 目录（chroot 内视角）：/opt/ag2a-console
# 不修改 /app、不修改 boot.sh、不修改 droidvm.env。
# 项目注册表 projects.json 属于运行时数据，重装时绝不覆盖。
# ============================================================
SRC=$(dirname "$(readlink -f "$0")")
R=/data/local/droidvm/rootfs
ROOT=$R/opt/ag2a-console
CROOT=/opt/ag2a-console
ENVV=/data/adb/droidvm.env
SERVICE_D=/data/adb/service.d
PORT=3077
PY_ENV="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root"

[ "$(id -u 2>/dev/null)" = "0" ] || { echo "需要 root：su -c 'sh $0'"; exit 1; }
[ -x "$R/usr/bin/python3" ] || { echo "未找到 chroot 内 python3：$R/usr/bin/python3"; exit 1; }
[ -x "$R/app/agent2api-server" ] || { echo "未找到 /app/agent2api-server，确认 DroidVM rootfs 路径"; exit 1; }

CONSOLE_PID() {
    /system/bin/pgrep -f 'ag2a-console.py' 2>/dev/null | while read -r _p; do
        [ "$_p" = "$$" ] && continue
        printf '%s\n' "$_p"
    done | head -n1
}

case "$1" in
  status)
    echo "root=$ROOT"
    echo "port=$PORT"
    echo "token=$(head -n1 "$ROOT/token" 2>/dev/null)"
    echo "pid=$(cat "$ROOT/console.pid" 2>/dev/null | head -n1)"
    echo "engine=$(cat "$ROOT/engine" 2>/dev/null | head -n1)"
    /system/bin/curl -s --noproxy '*' -o /dev/null -w "local_http=%{http_code}\n" --max-time 5 "http://127.0.0.1:$PORT/" 2>/dev/null
    exit 0;;
  uninstall)
    _p=$(head -n1 "$ROOT/console.pid" 2>/dev/null)
    case "$_p" in ''|*[!0-9]*) :;; *) kill "$_p" 2>/dev/null;; esac
    rm -rf "$ROOT" "$SERVICE_D/ag2a-console.sh" 2>/dev/null
    echo "控制台已移除（/app 与网关数据未动；Caddy 反代需另行移除）"
    exit 0;;
esac

echo "[1/7] 安装文件"
mkdir -p "$ROOT/www" "$ROOT/work/downloads" "$ROOT/work/backups" 2>/dev/null
cp -f "$SRC"/*.sh "$SRC"/*.py "$ROOT/" 2>/dev/null
cp -f "$SRC/www/index.html" "$ROOT/www/" 2>/dev/null
cp -f "$SRC/projects.default.json" "$ROOT/" 2>/dev/null
# 项目注册表是运行时数据（面板可增删改），只在首次安装时初始化，重装绝不覆盖
[ -s "$ROOT/projects.json" ] || cp -f "$SRC/projects.default.json" "$ROOT/projects.json" 2>/dev/null
chmod 0755 "$ROOT"/*.sh "$ROOT"/*.py 2>/dev/null
chmod 0700 "$ROOT" 2>/dev/null
mkdir -p "$ROOT/tasks" 2>/dev/null

# 令牌只在首次生成，重装保持不变
if [ ! -s "$ROOT/token" ]; then
    _t=$(od -An -N18 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' | head -c 36)
    [ -n "$_t" ] || _t="$(date +%s)$$"
    printf '%s\n' "$_t" > "$ROOT/token"
fi
chmod 0600 "$ROOT/token" 2>/dev/null
[ -f "$ROOT/auto.conf" ] || printf 'AUTO=off\nAT=04:00\nCHANNEL=beta\n' > "$ROOT/auto.conf"
[ -f "$ROOT/upstream.conf" ] || printf 'PROXY=http://127.0.0.1:7890\nMIRRORS=https://gh-proxy.com/https://github.com/;https://ghfast.top/https://github.com/\n' > "$ROOT/upstream.conf"
printf '{"running":false,"kind":"","result":"","error":""}\n' > "$ROOT/task.json" 2>/dev/null
printf '{"running":false,"kind":"","result":"","error":""}\n' > "$ROOT/wb-task.json" 2>/dev/null
[ -f "$ROOT/update.log" ] || : > "$ROOT/update.log" 2>/dev/null
printf 'python3-chroot\n' > "$ROOT/engine" 2>/dev/null

echo "[2/7] 生成网关启动凭据（只读 droidvm.env）"
if [ ! -s "$ROOT/gateway.env" ]; then
    _u=''; _p=''
    [ -r "$ENVV" ] && _u=$(sed -n "s/^A2A_USER=//p" "$ENVV" | head -n1 | sed "s/^['\"]//;s/['\"]$//")
    [ -r "$ENVV" ] && _p=$(sed -n "s/^A2A_PASS=//p" "$ENVV" | head -n1 | sed "s/^['\"]//;s/['\"]$//")
    [ -n "$_u" ] || _u=admin
    if [ -z "$_p" ]; then
        # 兜底：从正在运行的网关进程环境里取（不落盘明文到日志）
        _pid=$(/system/bin/pgrep -x agent2api-server 2>/dev/null | head -n1)
        [ -n "$_pid" ] && _p=$(tr '\0' '\n' < "/proc/$_pid/environ" 2>/dev/null | sed -n 's/^AGENT2API_ADMIN_PASSWORD=//p' | head -n1)
    fi
    [ -n "$_p" ] || { echo "! 无法取得网关管理员密码，请检查 $ENVV"; exit 1; }
    printf "AGENT2API_ADMIN_USER='%s'\nAGENT2API_ADMIN_PASSWORD='%s'\n" "$_u" "$_p" > "$ROOT/gateway.env"
    chmod 0600 "$ROOT/gateway.env" 2>/dev/null
    echo "  已生成（长度 ${#_p}）"
else
    echo "  已存在，保留"
fi

echo "[3/7] 记录当前版本"
_ver=${1:-${AG2A_LOCAL_VERSION:-}}
if [ -z "$_ver" ]; then
    # 从运行体二进制里找形如 3.4.2 的版本串
    _ver=$(strings "$R/app/agent2api-server" 2>/dev/null | grep -oE '^v?3\.[0-9]+\.[0-9]+(-[a-z]+\.[0-9]+)?$' | sort -V | tail -n1)
fi
[ -n "$_ver" ] || _ver=v3.4.2
case "$_ver" in v*) :;; *) _ver="v$_ver";; esac
printf 'version=%s\n' "$_ver" > "$ROOT/work/version" 2>/dev/null
echo "  本地版本 $_ver"

echo "[4/7] 停止旧实例"
for _q in $(/system/bin/pgrep -f 'ag2a-console.py' 2>/dev/null); do
    [ "$_q" = "$$" ] && continue
    kill "$_q" 2>/dev/null
done
sleep 1 2>/dev/null

echo "[5/7] 启动控制台（chroot 内 python3）"
# shellcheck disable=SC2086
chroot "$R" /usr/bin/env -i $PY_ENV \
    /bin/sh -c 'setsid nohup /usr/bin/python3 /opt/ag2a-console/ag2a-console.py >> /opt/ag2a-console/console.log 2>&1 < /dev/null &'
sleep 2 2>/dev/null
_cp=$(CONSOLE_PID)
printf '%s\n' "$_cp" > "$ROOT/console.pid" 2>/dev/null

echo "[6/7] 开机自启（等 chroot 挂载就绪后拉起）"
mkdir -p "$SERVICE_D" 2>/dev/null
cat > "$SERVICE_D/ag2a-console.sh" <<EOF
#!/system/bin/sh
# 由 项目更新中心/install.sh 生成：开机拉起更新控制台
(
  R=$R
  i=0
  while [ \$i -lt 90 ]; do
    [ -x "\$R/usr/bin/python3" ] && [ -f "\$R/opt/ag2a-console/ag2a-console.py" ] && break
    sleep 10; i=\$((i+1))
  done
  chroot "\$R" /usr/bin/env -i $PY_ENV \\
    /bin/sh -c 'setsid nohup /usr/bin/python3 /opt/ag2a-console/ag2a-console.py >> /opt/ag2a-console/console.log 2>&1 < /dev/null &'
) &
EOF
chmod 0755 "$SERVICE_D/ag2a-console.sh" 2>/dev/null

echo "[7/7] 验证"
_c=$(/system/bin/curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 6 "http://127.0.0.1:$PORT/" 2>/dev/null)
echo "engine=python3-chroot  pid=$_cp  local_http=$_c  port=$PORT"
echo "token=$(head -n1 "$ROOT/token" 2>/dev/null)"
echo "本机: http://127.0.0.1:$PORT/   公网: sh enable-remote.sh --apply"
if [ "$_c" != "200" ]; then
    echo "! 本地探活未通过，日志："
    tail -n 20 "$ROOT/console.log" 2>/dev/null
    exit 1
fi
