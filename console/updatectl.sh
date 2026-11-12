#!/bin/sh
# ============================================================
# Agent2API 远程更新执行器  v2.0
# 运行环境：DroidVM chroot 内、root、Debian 用户态（/bin/sh = dash）
#
# 适配的真实部署（与 Magisk 模块无关）：
#   运行体   /app/agent2api-server        单体 Go 二进制
#   前端     /app/ui                      静态文件
#   数据     /root/.agent2api/agent2api.db (+ -wal / -shm)
#   插件     /root/.agent2api/plugins
#   启动     chroot 内 env -i + setsid nohup ./agent2api-server
#
# 控制平面 /opt/ag2a-console 独立于 /app，更新时不会被替换。
#
# 命令：
#   status                 输出 key=value 状态
#   check [beta|stable]    查询 GitHub releases，写 latest
#   download [tag]         多源下载 + sha256 严格校验 + 解包预检
#   apply <tar.gz>         备份 → 停服 → 原子替换 → 启动 → 健康检查 → 失败自动回滚
#   rollback [备份ID]       恢复二进制 / ui / 数据库 / 插件并重启
#   auto on|off|HH:MM|channel beta|stable|status
#   logs [行数] / backups
#
# 不依赖智能体、不依赖 adb、不依赖网关登录态。
# ============================================================

APP=/app
DATA=/root/.agent2api
CONSOLE=/opt/ag2a-console
WORK=$CONSOLE/work
DL=$WORK/downloads
BK=$WORK/backups
LOCK=$WORK/update.lock
STATE=$CONSOLE/state
LATEST=$CONSOLE/latest
PROGRESS=$CONSOLE/progress
LOG=$CONSOLE/update.log
CONF=$CONSOLE/auto.conf
GWENV=$CONSOLE/gateway.env
PORT=3065
PROXY=http://127.0.0.1:7890
PY=/usr/bin/python3
CURL=/usr/bin/curl
TAR=/bin/tar
SHA=/usr/bin/sha256sum

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
mkdir -p "$WORK" "$DL" "$BK" "$CONSOLE" 2>/dev/null

log() {
    _t=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    printf '%s %s\n' "$_t" "$*" >> "$LOG" 2>/dev/null
    printf '%s %s\n' "$_t" "$*"
}
progress() { printf '%s|%s|%s\n' "$1" "$2" "$3" > "$PROGRESS" 2>/dev/null; log "[$1 $2%] $3"; }
setkv() { _f=$1; shift; : > "$_f" 2>/dev/null; for _p in "$@"; do printf '%s\n' "$_p" >> "$_f" 2>/dev/null; done; }
getkv() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n1; }

# 上游通道（面板可改）：只影响 check/download，不触碰全局代理配置
UPCONF=$CONSOLE/upstream.conf
MIRRORS="https://gh-proxy.com/https://github.com/;https://ghfast.top/https://github.com/"
_p=$(getkv PROXY "$UPCONF" 2>/dev/null); [ -n "$_p" ] && PROXY="$_p"
_m=$(getkv MIRRORS "$UPCONF" 2>/dev/null); [ -n "$_m" ] && MIRRORS="$_m"
src_list() { printf '%s' "$MIRRORS" | tr ';' '\n' | sed '/^[[:space:]]*$/d'; }

# 只操作白名单目录，防止误删
safe_path() {
    case "$1" in
        "$WORK"/*|"$CONSOLE"/*|"$APP"/*) return 0 ;;
        *) log "拒绝操作非白名单路径: $1"; return 1 ;;
    esac
}

# 下载：每个 URL 先走本机 clash 代理，再直连；url 可带镜像前缀
fetch() {
    _u=$1; _o=$2; _t=${3:-300}
    rm -f "$_o" 2>/dev/null
    # 注意 --noproxy 的参数不能放进未加引号的变量里，否则 * 会被 shell glob 展开
    if $CURL -fsSL --connect-timeout 15 --max-time "$_t" --proxy "$PROXY" \
            -o "$_o" "$_u" 2>/dev/null && [ -s "$_o" ]; then return 0; fi
    rm -f "$_o" 2>/dev/null
    if $CURL -fsSL --connect-timeout 15 --max-time "$_t" --noproxy '*' \
            -o "$_o" "$_u" 2>/dev/null && [ -s "$_o" ]; then return 0; fi
    rm -f "$_o" 2>/dev/null
    return 1
}

sha_of() { [ -f "$1" ] && $SHA "$1" 2>/dev/null | awk '{print $1}'; }

# 端口 LISTEN 判定（不依赖 curl）
port_listen() {
    _h=$(printf '%04X' "$PORT" 2>/dev/null)
    awk -v h="$_h" 'FNR==1{next} $4=="0A"{n=split($2,a,":"); if(a[n]==h){f=1;exit}} END{exit(f?0:1)}' \
        /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

# 网关 HTTP 探活；--noproxy 必须加，否则会被本机透明代理劫持
http_ok() {
    _c=$($CURL -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:$PORT/api/panel/status" 2>/dev/null)
    [ "$_c" = "200" ] && return 0
    return 1
}

# 按 /proc/*/cmdline 找进程。不能用 pgrep -x：Linux comm 只保留 15 字符，
# 'agent2api-server'(16) 被截成 'agent2api-serve'，pgrep -x 永远匹配不到，
# 会导致「以为没进程 → 不停服 → 新进程起不来 → 旧进程 200 假阳性」。
gw_pids() {
    for _d in /proc/[0-9]*; do
        _p=${_d##*/}
        [ -r "$_d/cmdline" ] || continue
        _c=$(tr '\0' ' ' < "$_d/cmdline" 2>/dev/null) 2>/dev/null
        case "$_c" in
            *agent2api-server*|*agent2api-providers*|*agent2api-web-providers*) printf '%s ' "$_p" ;;
        esac
    done
}

# 按 /proc 精确停服；必须确认端口真的关闭后才返回成功
stop_gateway() {
    _pids=$(gw_pids)
    [ -z "$_pids" ] && { log "网关未运行，跳过停止"; return 0; }
    log "停止网关进程: $_pids"
    # shellcheck disable=SC2086
    /bin/kill -TERM $_pids 2>/dev/null
    _i=0
    while [ "$_i" -lt 10 ]; do
        port_listen || break
        sleep 1 2>/dev/null; _i=$((_i+1))
    done
    _left=$(gw_pids)
    if [ -n "$_left" ]; then
        log "强制结束残留: $_left"
        # shellcheck disable=SC2086
        /bin/kill -KILL $_left 2>/dev/null
        sleep 1 2>/dev/null
    fi
    if port_listen; then
        log "端口 $PORT 仍被占用，停止失败"
        return 1
    fi
    log "网关已停止，端口 $PORT 已释放"
    return 0
}

# 与 DroidVM boot.sh 完全一致的启动方式（凭据由 install.sh 落到 gateway.env）
start_gateway() {
    [ -r "$GWENV" ] || { log "缺少 $GWENV，无法启动网关"; return 1; }
    . "$GWENV"
    [ -n "$AGENT2API_ADMIN_PASSWORD" ] || { log "gateway.env 未含管理员密码"; return 1; }
    /usr/bin/env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
        AGENT2API_ADMIN_USER="${AGENT2API_ADMIN_USER:-admin}" \
        AGENT2API_ADMIN_PASSWORD="$AGENT2API_ADMIN_PASSWORD" \
        AGENT2API_HOST=0.0.0.0 AGENT2API_UI_DIR=/app/ui AGENT2API_CAPTCHA_ENABLED=0 \
        /bin/sh -c 'cd /app && setsid nohup ./agent2api-server >> /app/a2a.log 2>&1 < /dev/null &'
    return 0
}

wait_healthy() {
    _max=${1:-30}; _i=0
    while [ "$_i" -lt "$_max" ]; do
        http_ok && return 0
        sleep 2 2>/dev/null; _i=$((_i+1))
    done
    return 1
}

# ---------- 状态 ----------
do_status() {
    _ver=$(getkv version "$WORK/version"); [ -n "$_ver" ] || _ver=unknown
    _run=0; port_listen && _run=1
    _auto=off; [ -f "$CONF" ] && _auto=$(getkv AUTO "$CONF")
    _at=$(getkv AT "$CONF"); [ -n "$_at" ] || _at=04:00
    _ch=$(getkv CHANNEL "$CONF"); [ -n "$_ch" ] || _ch=beta
    _last=$(getkv last_result "$STATE")
    _bsize=$(wc -c < "$APP/agent2api-server" 2>/dev/null | tr -d ' ')
    _dsize=$(wc -c < "$DATA/agent2api.db" 2>/dev/null | tr -d ' ')
    _nui=$(find "$APP/ui" -type f 2>/dev/null | wc -l | tr -d ' ')
    setkv "$STATE" \
        "version=$_ver" "port=$PORT" "running=$_run" "binary_size=$_bsize" \
        "db_size=$_dsize" "ui_files=$_nui" "auto=$_auto" "auto_at=$_at" "channel=$_ch" \
        "last_result=$_last" "last_version=$(getkv last_version "$STATE")" \
        "last_backup=$(getkv last_backup "$STATE")" \
        "timestamp=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)"
    cat "$STATE" 2>/dev/null
    printf 'progress_file=%s\nlog_file=%s\nbackups=%s\n' "$PROGRESS" "$LOG" \
        "$(ls -1 "$BK" 2>/dev/null | grep -c '^bk-' | tr -d ' ')"
}

# ---------- 查更新 ----------
do_check() {
    _ch=${1:-$(getkv CHANNEL "$CONF")}; [ -n "$_ch" ] || _ch=beta
    progress check 10 "查询 GitHub releases ($_ch)"
    _tmp="$WORK/.check.out"
    $PY "$CONSOLE/upstream.py" "$_ch" > "$_tmp" 2>>"$LOG"
    _rc=$?
    if [ "$_rc" != "0" ] || grep -q '^error=' "$_tmp" 2>/dev/null; then
        # 兜底：上游被网络策略阻断时，若已有本地版本清单（预置或面板上传），
        # 仍然允许继续更新；日志明确标注来源，不伪装成在线检查成功。
        if [ -s "$LATEST" ] && grep -q '^version=' "$LATEST" 2>/dev/null \
                && grep -q '^has_update=1' "$LATEST" 2>/dev/null; then
            # 离线清单是静态的，必须按「当前版本」重算 has_update，
            # 否则更新完成后仍显示有更新、定时更新会反复重装同一版本。
            _cur=$(getkv version "$WORK/version")
            _lv=$(getkv version "$LATEST")
            _has=0
            if [ -n "$_lv" ] && [ "$_lv" != "$_cur" ]; then
                _min=$(printf '%s\n%s\n' "$_cur" "$_lv" | /usr/bin/sort -V 2>/dev/null | head -n1)
                [ "$_min" = "$_cur" ] && _has=1
            fi
            sed "s/^has_update=.*/has_update=$_has/" "$LATEST" > "$WORK/.latest.tmp" 2>/dev/null \
                && mv -f "$WORK/.latest.tmp" "$LATEST" 2>/dev/null
            progress check 100 "上游不可达，使用本地版本清单 $_lv（本地 $_cur）"
            # 注意：这里不能出现 error= 字样，控制台会把检查输出中的 error= 判为失败
            log "在线检查未成功，已回退本地版本清单"
            cat "$LATEST" 2>/dev/null
            printf 'source=cache\n'
            rm -f "$_tmp" 2>/dev/null
            return 0
        fi
        progress check 100 "上游查询失败"
        cat "$_tmp" 2>/dev/null
        rm -f "$_tmp" 2>/dev/null
        return 1
    fi
    cp -f "$_tmp" "$LATEST" 2>/dev/null
    rm -f "$_tmp" 2>/dev/null
    progress check 100 "本地 $(getkv local "$LATEST") / 上游 $(getkv version "$LATEST")"
    cat "$LATEST" 2>/dev/null
}

# ---------- 下载 + 校验 ----------
do_download() {
    _tag=$1
    [ -n "$_tag" ] || _tag=$(getkv tag "$LATEST")
    [ -n "$_tag" ] || { log "没有指定版本，请先 check"; return 1; }
    _asset=$(getkv assetUrl "$LATEST")
    _shaurl=$(getkv shaUrl "$LATEST")
    _name=$(getkv assetName "$LATEST"); [ -n "$_name" ] || _name=agent2api-server-linux-arm64.tar.gz
    [ -n "$_asset" ] || _asset="https://github.com/aimod-cc/Agent2API/releases/download/$_tag/$_name"
    [ -n "$_shaurl" ] || _shaurl="$_asset.sha256"

    _out="$DL/$_tag.tar.gz"
    if [ -s "$_out" ] && [ -f "$DL/$_tag.sha256" ]; then
        log "已缓存 $_out"
        echo "pkg=$_out"; return 0
    fi
    progress download 5 "开始下载 $_tag"

    # 先取校验文件（很小），多源
    _shaok=0
    for _s in "" $(src_list); do
        if [ -z "$_s" ]; then _su="$_shaurl"; else _su="$_s${_shaurl#https://github.com/}"; fi
        if fetch "$_su" "$DL/$_tag.sha256" 45; then _shaok=1; break; fi
    done
    [ "$_shaok" = "1" ] || { progress download 100 "校验文件下载失败"; return 1; }
    _expect=$(awk '{print $1}' "$DL/$_tag.sha256" 2>/dev/null | head -n1 | tr 'A-Z' 'a-z')
    case "$_expect" in
        ''|*[!0-9a-f]*) progress download 100 "sha256 文件内容异常"; return 1 ;;
    esac
    [ "${#_expect}" -eq 64 ] 2>/dev/null || { progress download 100 "sha256 长度异常"; return 1; }

    # 下载主体：代理直连优先，其次两个镜像（每个都再试直连）
    _ok=0
    for _s in "" $(src_list); do
        if [ -z "$_s" ]; then _u="$_asset"; else _u="$_s${_asset#https://github.com/}"; fi
        log "尝试下载源: $_u"
        if fetch "$_u" "$_out" 600; then _ok=1; break; fi
    done
    [ "$_ok" = "1" ] || { progress download 100 "全部下载源失败"; return 1; }

    _sz=$(wc -c < "$_out" 2>/dev/null | tr -d ' ')
    [ "$_sz" -gt 5000000 ] 2>/dev/null || { rm -f "$_out"; progress download 100 "包体积异常 $_sz"; return 1; }
    _got=$(sha_of "$_out" | tr 'A-Z' 'a-z')
    [ "$_got" = "$_expect" ] || {
        log "sha256 不匹配: local=$_got expect=$_expect"
        rm -f "$_out" 2>/dev/null
        progress download 100 "sha256 校验失败，已丢弃"; return 1
    }
    progress download 60 "sha256 校验通过 $(( _sz / 1024 )) KB，开始解包预检"

    # 解包预检：必须有可执行二进制与 ui/index.html
    _stage="$WORK/.stage-$_tag"
    safe_path "$_stage" || return 1
    rm -rf "$_stage" 2>/dev/null; mkdir -p "$_stage" 2>/dev/null
    $TAR -xzf "$_out" -C "$_stage" 2>>"$LOG" || { rm -rf "$_stage"; rm -f "$_out"; progress download 100 "解包失败"; return 1; }
    [ -s "$_stage/agent2api-server" ] && [ -f "$_stage/ui/index.html" ] || {
        log "包结构不完整"; rm -rf "$_stage"; rm -f "$_out"; progress download 100 "包结构不完整"; return 1
    }
    chmod 0755 "$_stage/agent2api-server" 2>/dev/null
    progress download 100 "下载完成，包已就绪"
    echo "pkg=$_out"
}

# ---------- 备份 ----------
do_backup() {
    _id="bk-$(date '+%Y%m%d-%H%M%S' 2>/dev/null)"
    _d="$BK/$_id"
    safe_path "$_d" || return 1
    mkdir -p "$_d" 2>/dev/null || return 1

    cp -f "$APP/agent2api-server" "$_d/agent2api-server" 2>/dev/null || return 1
    $TAR -cf "$_d/ui.tar" -C "$APP" ui 2>>"$LOG" || return 1
    [ -d "$DATA/plugins" ] && $TAR -cf "$_d/plugins.tar" -C "$DATA" plugins 2>>"$LOG"
    printf '%s\n' "$(getkv version "$WORK/version")" > "$_d/version" 2>/dev/null

    # SQLite 一致性快照（含 WAL，不能用 cp 主库）
    if [ -f "$DATA/agent2api.db" ]; then
        $PY -c '
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
' "$DATA/agent2api.db" "$_d/agent2api.db" 2>>"$LOG" || { log "数据库快照失败"; return 1; }
    fi
    echo "$_id" > "$CONSOLE/last_backup" 2>/dev/null
    echo "$_id"
}

# ---------- 应用（核心） ----------
do_apply() {
    _pkg=$1
    [ -s "$_pkg" ] || { log "包不存在: $_pkg"; return 1; }
    # 陈旧锁自愈
    if [ -f "$LOCK" ]; then
        _lp=$(head -n1 "$LOCK" 2>/dev/null)
        case "$_lp" in
            ''|*[!0-9]*) rm -f "$LOCK" 2>/dev/null ;;
            *) /bin/kill -0 "$_lp" 2>/dev/null || rm -f "$LOCK" 2>/dev/null ;;
        esac
    fi
    [ -f "$LOCK" ] && { log "已有更新任务在跑"; return 2; }
    printf '%s\n' "$$" > "$LOCK" 2>/dev/null

    _tag=$(getkv tag "$LATEST")
    _fail() {
        progress apply "$1" "$2"; log "失败: $2"
        do_rollback "$(cat "$CONSOLE/last_backup" 2>/dev/null)" >/dev/null 2>&1
        rm -f "$LOCK" 2>/dev/null
        setkv "$STATE" "last_result=failed" "last_error=$2" \
            "last_version=$(getkv last_version "$STATE")" "last_backup=$(getkv last_backup "$STATE")"
        return 1
    }

    progress apply 5 "预检与备份"
    _bid=$(do_backup) || { _fail 5 "备份失败"; return 1; }
    log "备份完成 $_bid"

    # 定位已预检的解包目录（download 生成；缺失则现场解包）
    _stage="$WORK/.stage-$_tag"
    if [ ! -s "$_stage/agent2api-server" ]; then
        safe_path "$_stage" || { rm -f "$LOCK"; return 1; }
        rm -rf "$_stage" 2>/dev/null; mkdir -p "$_stage" 2>/dev/null
        $TAR -xzf "$_pkg" -C "$_stage" 2>>"$LOG" || { _fail 10 "解包失败"; return 1; }
        [ -s "$_stage/agent2api-server" ] && [ -f "$_stage/ui/index.html" ] || { _fail 10 "包结构不完整"; return 1; }
        chmod 0755 "$_stage/agent2api-server" 2>/dev/null
    fi
    _local=$(getkv version "$WORK/version")
    if [ -n "$_local" ] && [ "$_local" != "unknown" ]; then
        _lo=$(printf '%s\n%s\n' "$_local" "$_tag" | /usr/bin/sort -V 2>/dev/null | head -n1)
        [ "$_lo" = "$_tag" ] && [ "$_tag" != "$_local" ] && { _fail 10 "拒绝降级：$_tag < $_local"; return 1; }
    fi
    progress apply 25 "包校验通过，停止网关"
    _oldpids=$(gw_pids)
    log "更新前网关进程: $_oldpids"
    stop_gateway >> "$LOG" 2>&1 || { _fail 30 "停止网关失败（端口未释放）"; return 1; }
    sleep 1 2>/dev/null

    # 二进制：写 .new 后原子 mv
    cp -f "$_stage/agent2api-server" "$APP/agent2api-server.new" 2>/dev/null || { _fail 35 "复制二进制失败"; return 1; }
    chmod 0755 "$APP/agent2api-server.new" 2>/dev/null
    mv -f "$APP/agent2api-server.new" "$APP/agent2api-server" 2>/dev/null || { _fail 35 "替换二进制失败"; return 1; }
    progress apply 45 "二进制已替换，替换前端"

    # ui：整目录交换，保留 ui.old 供本步失败即时还原
    rm -rf "$APP/ui.new" "$APP/ui.old" 2>/dev/null
    cp -R "$_stage/ui" "$APP/ui.new" 2>/dev/null || { _fail 50 "复制 ui 失败"; return 1; }
    if [ -d "$APP/ui" ]; then mv "$APP/ui" "$APP/ui.old" 2>/dev/null; fi
    mv "$APP/ui.new" "$APP/ui" 2>/dev/null || {
        [ -d "$APP/ui.old" ] && mv "$APP/ui.old" "$APP/ui" 2>/dev/null
        _fail 50 "替换 ui 失败"; return 1
    }
    progress apply 60 "启动新版网关"

    start_gateway >> "$LOG" 2>&1 || { _fail 65 "启动命令执行失败"; return 1; }

    # 必须出现「不在旧 PID 集合中」的新进程，否则说明旧进程还占着端口、
    # 新进程启动失败，此时下面的 HTTP 200 会是假阳性。
    _newok=0; _i=0
    while [ "$_i" -lt 12 ]; do
        _newp=""
        for _q in $(gw_pids); do
            case " $_oldpids " in
                *" $_q "*) : ;;
                *) _newp="$_newp $_q" ;;
            esac
        done
        [ -n "$_newp" ] && { _newok=1; break; }
        sleep 2 2>/dev/null; _i=$((_i+1))
    done
    if [ "$_newok" != "1" ]; then
        _fail 68 "新版进程未启动（端口可能被残留进程占用）"; return 1
    fi
    log "新版进程已启动:$_newp（旧:$_oldpids）"

    progress apply 70 "健康检查（最多 60 秒）"
    if ! wait_healthy 30; then
        _fail 80 "新版启动后健康检查未通过（端口 $PORT）"; return 1
    fi

    # 提交
    printf 'version=%s\n' "$_tag" > "$WORK/version" 2>/dev/null
    rm -rf "$APP/ui.old" "$_stage" 2>/dev/null
    rm -f "$LOCK" 2>/dev/null
    setkv "$STATE" "last_result=success" "last_version=$_tag" "last_backup=$_bid" \
        "last_time=$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null)" \
        "auto=$(getkv AUTO "$CONF")" "auto_at=$(getkv AT "$CONF")" "channel=$(getkv CHANNEL "$CONF")"
    progress apply 100 "更新完成 $_tag，备份 $_bid"
    echo "ok=1 backup=$_bid version=$_tag"
}

# ---------- 回滚 ----------
do_rollback() {
    _id=$1
    [ -n "$_id" ] || _id=$(cat "$CONSOLE/last_backup" 2>/dev/null)
    _d="$BK/$_id"
    [ -d "$_d" ] || { log "备份不存在: $_id"; return 1; }
    safe_path "$_d" || return 1
    progress rollback 10 "恢复备份 $_id"

    stop_gateway >> "$LOG" 2>&1
    sleep 1 2>/dev/null

    [ -f "$_d/agent2api-server" ] && cp -f "$_d/agent2api-server" "$APP/agent2api-server" 2>/dev/null
    chmod 0755 "$APP/agent2api-server" 2>/dev/null
    if [ -f "$_d/ui.tar" ]; then
        rm -rf "$APP/ui" 2>/dev/null
        $TAR -xf "$_d/ui.tar" -C "$APP" 2>>"$LOG" || return 1
    fi
    if [ -f "$_d/plugins.tar" ]; then
        rm -rf "$DATA/plugins" 2>/dev/null
        $TAR -xf "$_d/plugins.tar" -C "$DATA" 2>>"$LOG"
    fi
    # 恢复数据库：必须一并清掉 WAL/SHM，否则旧 WAL 会覆盖主库
    if [ -f "$_d/agent2api.db" ]; then
        cp -f "$_d/agent2api.db" "$DATA/agent2api.db" 2>/dev/null
        rm -f "$DATA/agent2api.db-wal" "$DATA/agent2api.db-shm" 2>/dev/null
    fi
    [ -s "$_d/version" ] && printf 'version=%s\n' "$(head -n1 "$_d/version" 2>/dev/null)" > "$WORK/version" 2>/dev/null

    progress rollback 60 "文件已恢复，重启网关"
    start_gateway >> "$LOG" 2>&1
    wait_healthy 20 || { progress rollback 100 "回滚后健康检查未通过"; return 1; }
    setkv "$STATE" "last_result=rolled_back" "last_backup=$_id" "last_version=$(getkv version "$WORK/version")"
    progress rollback 100 "已回滚到 $_id"
    echo "ok=1 rollback=$_id"
}

# ---------- 自动更新配置 ----------
do_auto() {
    [ -f "$CONF" ] || printf 'AUTO=off\nAT=04:00\nCHANNEL=beta\n' > "$CONF" 2>/dev/null
    case "$1" in
        on|off) sed -i "s/^AUTO=.*/AUTO=$1/" "$CONF" 2>/dev/null ;;
        [0-2][0-9]:[0-5][0-9]) sed -i "s|^AT=.*|AT=$1|" "$CONF" 2>/dev/null ;;
        channel) [ -n "$2" ] && sed -i "s|^CHANNEL=.*|CHANNEL=$2|" "$CONF" 2>/dev/null ;;
        status) : ;;
        *) echo "用法: auto on|off|HH:MM|channel beta|stable|status"; return 1 ;;
    esac
    cat "$CONF" 2>/dev/null
}

case "$1" in
    status)   do_status ;;
    check)    do_check "$2" ;;
    download) do_download "$2" ;;
    apply)    do_apply "$2" ;;
    rollback) do_rollback "$2" ;;
    auto)     do_auto "$2" "$3" ;;
    logs)     tail -n "${2:-80}" "$LOG" 2>/dev/null ;;
    backups)  ls -1t "$BK" 2>/dev/null | grep '^bk-' | head -n 20 ;;
    *) echo "用法: $0 status|check|download|apply|rollback|auto|logs|backups"; exit 1 ;;
esac
