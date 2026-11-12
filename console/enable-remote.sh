#!/system/bin/sh
# ============================================================
# 项目更新中心 / Project Update Hub · 公网入口（Caddy 反代 /ag2a/*）
# 在 Android 层以 root 执行。
#
# 注意：MARK 是 Caddyfile 里的历史标识，用于幂等判断，切勿修改，
#       否则会把已存在的反代当成不存在而重复插入。
#
#   sh enable-remote.sh              dry-run：只打印将要改动的内容
#   sh enable-remote.sh --apply      备份 → 写入 → caddy validate → 重启 → 探活
#   sh enable-remote.sh --remove     移除反代并重启
#
# 注意：Caddy 配了 admin off，无法用 caddy reload(API)，只能重启进程；
#       重启会短暂（1~2 秒）中断全部站点，脚本会自动验证并在失败时恢复配置。
# ============================================================
R=/data/local/droidvm/rootfs
CFG=$R/etc/caddy/Caddyfile
ENVV=/data/adb/droidvm.env
PORT=3077
SITE=https://dash.example.com
STAMP=$(date '+%Y%m%d-%H%M%S' 2>/dev/null)
MARK='===== Agent2API 远程更新控制台（由 enable-remote.sh 生成）====='

[ "$(id -u 2>/dev/null)" = "0" ] || { echo "需要 root：su -c 'sh $0 --apply'"; exit 1; }
[ -f "$CFG" ] || { echo "Caddyfile 不存在: $CFG"; exit 1; }

BLOCK='	# '"$MARK"'
	handle_path /ag2a/* {
		reverse_proxy 127.0.0.1:'"$PORT"' {
			header_up X-Real-IP {remote_host}
		}
	}
'

PATCH=/opt/ag2a-console/caddy_patch.py
NEW=/etc/caddy/Caddyfile.new

patch_cfg() {
    # $1 = add|remove|check，生成文件在 chroot 内 $NEW
    chroot "$R" /usr/bin/python3 "$PATCH" "$1" /etc/caddy/Caddyfile "$NEW" 2>&1
}
cfg_present() {
    grep -q "$MARK" "$CFG" 2>/dev/null
}

env_load() {
    CF_API_TOKEN=''; HTTPS_PROXY=''; HTTP_PROXY=''; NO_PROXY=''
    [ -r "$ENVV" ] && . "$ENVV"
}

restart_caddy() {
    env_load
    # [c] 写法避免 pkill -f 匹配到自身命令行
    /system/bin/pkill -f '[c]addy run' 2>/dev/null
    sleep 1 2>/dev/null
    # shellcheck disable=SC2086
    chroot "$R" /usr/bin/env -i \
        PATH=/usr/local/bin:/usr/bin:/bin HOME=/root \
        XDG_DATA_HOME=/var/lib/caddy XDG_CONFIG_HOME=/etc/caddy \
        CF_API_TOKEN="$CF_API_TOKEN" \
        HTTPS_PROXY="$HTTPS_PROXY" HTTP_PROXY="$HTTP_PROXY" NO_PROXY="$NO_PROXY" \
        /bin/sh -c 'setsid nohup /usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >> /var/log/caddy/caddy.out 2>&1 < /dev/null &'
    sleep 3 2>/dev/null
}

validate_cfg() {
    env_load
    # shellcheck disable=SC2086
    chroot "$R" /usr/bin/env -i \
        PATH=/usr/local/bin:/usr/bin:/bin HOME=/root \
        CF_API_TOKEN="$CF_API_TOKEN" HTTPS_PROXY="$HTTPS_PROXY" \
        /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1
}

case "$1" in
  --remove)
    cfg_present || { echo "未发现 /ag2a 反代，无需移除"; exit 0; }
    cp -f "$CFG" "$CFG.bak-ag2a-$STAMP" 2>/dev/null || exit 1
    patch_cfg remove | grep -q '^ok$' || { echo "生成失败"; rm -f "$R$NEW"; exit 1; }
    mv -f "$R$NEW" "$CFG" 2>/dev/null || exit 1
    chmod 0644 "$CFG" 2>/dev/null
    restart_caddy
    echo "已移除 /ag2a 反代（备份 $CFG.bak-ag2a-$STAMP）"
    exit 0;;
esac

echo "=== 将要插入 $SITE 块的内容 ==="
printf '%s\n' "$BLOCK"
echo "=== 目标位置：该站点块内第一个 reverse_proxy 127.0.0.1:8765 之前 ==="
if cfg_present; then
    echo "已存在 /ag2a 反代，无需改动（幂等）"
    /system/bin/curl -s --noproxy '*' -o /dev/null -w "  控制台本体 http://127.0.0.1:$PORT/ -> %{http_code}\n" --max-time 5 "http://127.0.0.1:$PORT/" 2>/dev/null
    exit 0
fi
[ "$1" = "--apply" ] || { echo "这是 dry-run；加 --apply 才会实际改动。"; exit 0; }

echo "=== [1/5] 备份 ==="
cp -f "$CFG" "$CFG.bak-ag2a-$STAMP" 2>/dev/null || { echo "备份失败"; exit 1; }
echo "  $CFG.bak-ag2a-$STAMP"

echo "=== [2/5] 生成新配置（chroot 内 Python）==="
_p=$(patch_cfg add)
printf '%s\n' "$_p"
if ! printf '%s' "$_p" | grep -q '^ok$' || [ ! -s "$R$NEW" ]; then
    echo "生成失败（$_p）"; rm -f "$R$NEW"; exit 1
fi
mv -f "$R$NEW" "$CFG" 2>/dev/null || exit 1
chmod 0644 "$CFG" 2>/dev/null

echo "=== [3/5] caddy validate ==="
_v=$(validate_cfg)
if printf '%s' "$_v" | grep -qi 'valid\|success'; then
    echo "  OK"
else
    echo "  validate 输出：$(printf '%s' "$_v" | tail -n2 | tr '\n' ' ')"
fi

echo "=== [4/5] 重启 Caddy ==="
restart_caddy

echo "=== [5/5] 探活（失败自动恢复配置并重启）==="
_l=$(/system/bin/curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/" 2>/dev/null)
_c=$(/system/bin/curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 8 \
    -k --resolve dash.example.com:443:127.0.0.1 "https://dash.example.com/ag2a/" 2>/dev/null)
echo "  控制台本体=$_l  经 Caddy /ag2a/=$_c"
if [ "$_l" != "200" ]; then
    echo "! 控制台本体未就绪，检查 /opt/ag2a-console/console.log"
    exit 1
fi
case "$_c" in
    200|301|302|308) echo "完成：公网入口 https://dash.example.com/ag2a/  （首次需输入 token）" ;;
    *)
        echo "! 反代探活失败，恢复原配置"
        cp -f "$CFG.bak-ag2a-$STAMP" "$CFG" 2>/dev/null
        restart_caddy
        exit 1;;
esac
