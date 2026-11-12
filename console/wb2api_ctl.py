#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy2API 更新控制器（运行在 DroidVM chroot 内，只用标准库）

控制台本体在 chroot 内，而 wb2api 模块在 Android 层。借助 /proc/1/root
跨层访问 Android 根文件系统，无需额外守护进程或 adb。

命令：status / check / update [tag] / enable / disable / rollback
输出：key=value（status/check）或 ok=1 摘要，失败打印 error=...
"""
import json
import os
import re
import signal
import shutil
import sys
import time
import urllib.request
import zipfile

ANDROID = "/proc/1/root"
MOD = ANDROID + "/data/adb/modules/wb2api"
DATA = ANDROID + "/data/adb/wb2api"
PAYLOAD = MOD + "/payload/wb2api"
PREV = MOD + "/payload/wb2api.prev"
VERSION = MOD + "/VERSION"
PROP = MOD + "/module.prop"
DISABLE = MOD + "/disable"
SECRET = ANDROID + "/data/adb/wb2api.secret"
REPO = "misakano7545/workbuddy2api-panel"
ATOM = "https://github.com/%s/releases.atom" % REPO
CONSOLE = os.environ.get("AG2A_CONSOLE_DIR", "/opt/ag2a-console")
BK = os.path.join(CONSOLE, "work", "backups")
CONF = os.path.join(CONSOLE, "upstream.conf")
UCONF = os.path.join(CONSOLE, "wb-latest")
PROGRESS = os.path.join(CONSOLE, "progress.wb")
LOG = os.path.join(CONSOLE, "wb2api.log")
DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_MIRRORS = [
    "https://gh-proxy.com/https://github.com/",
    "https://ghfast.top/https://github.com/",
]
PROC_PAT = "/data/adb/modules/wb2api/payload/wb2api"


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def progress(step, pct, msg):
    try:
        with open(PROGRESS, "w", encoding="utf-8") as f:
            f.write("%s|%s|%s" % (step, pct, msg))
    except Exception:
        pass


def read_text(p, default=""):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def read_kv(path):
    out = {}
    for line in read_text(path).splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def proxy():
    c = read_kv(CONF)
    return c.get("PROXY") or os.environ.get("AG2A_PROXY") or DEFAULT_PROXY


def mirrors():
    c = read_kv(CONF)
    raw = c.get("MIRRORS")
    return [x.strip() for x in raw.split(";") if x.strip()] if raw else DEFAULT_MIRRORS


def http_get(url, timeout=30):
    px = proxy()
    last = ""
    for use in (True, False):
        try:
            if use and px:
                op = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": px, "https": px}))
            else:
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(url, headers={"User-Agent": "wb2api-console/1.0"})
            with op.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = "%s: %s" % ("proxy" if use else "direct", e)
    raise RuntimeError(last or "unreachable")


def pids():
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if PROC_PAT in cmd and "wb2api_ctl.py" not in cmd:
                out.append(int(name))
        except Exception:
            pass
    return out


def stop():
    ps = pids()
    for p in ps:
        try:
            os.kill(p, signal.SIGTERM)
        except Exception:
            pass
    for _ in range(10):
        if not pids():
            return True
        time.sleep(0.5)
    for p in pids():
        try:
            os.kill(p, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.5)
    return not pids()


def start():
    """在 Android 根命名空间里执行模块自带 start.sh。"""
    if os.path.exists(DISABLE):
        return False, "模块处于停用状态，未启动"
    script = "/data/adb/modules/wb2api/common/start.sh"
    try:
        p = __import__("subprocess").run(
            ["/usr/sbin/chroot", ANDROID, "/system/bin/sh", script],
            stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").STDOUT, timeout=40)
        ok = pids() != []
        return ok, (p.stdout or b"").decode("utf-8", "replace")[-200:]
    except Exception as e:
        return False, str(e)


def vkey(v):
    v = (v or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$", v)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0, 0)


def latest_tag():
    """用 releases.atom 取最新 tag（github.com 可用；api.github.com 出口被 403）。"""
    urls = [ATOM] + [m + ATOM for m in mirrors()]
    errs = []
    for u in urls:
        try:
            data = http_get(u, timeout=25)
            import xml.etree.ElementTree as ET
            ns = {"a": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(data)
            for e in root.findall("a:entry", ns):
                for link in e.findall("a:link", ns):
                    href = link.get("href", "")
                    if "/tag/" in href:
                        return href.rstrip("/").rsplit("/", 1)[-1], u
        except Exception as ex:
            errs.append("%s:%s" % (u.split("/")[2], str(ex)[:60]))
    raise RuntimeError("; ".join(errs) or "no tag")


def status():
    cur = read_text(VERSION).strip()
    lat = read_kv(UCONF).get("version", "")
    has = "1" if lat and vkey(lat) > vkey(cur) else "0"
    size = 0
    try:
        size = os.path.getsize(PAYLOAD)
    except Exception:
        pass
    out = {
        "version": cur,
        "latest": lat,
        "has_update": has,
        "running": "1" if pids() else "0",
        "enabled": "0" if os.path.exists(DISABLE) else "1",
        "binary_size": str(size),
        "prev_available": "1" if os.path.exists(PREV) else "0",
        "data_present": "1" if os.path.isdir(DATA) else "0",
        "secret_present": "1" if os.path.exists(SECRET) else "0",
    }
    pr = read_text(PROGRESS).strip().split("|")
    out["progress"] = "%s|%s|%s" % (pr[0], pr[1], pr[2]) if len(pr) >= 3 else ""
    for k, v in out.items():
        print("%s=%s" % (k, v))


def check():
    progress("check", "20", "查询上游 releases")
    tag, src = latest_tag()
    cur = read_text(VERSION).strip()
    has = "1" if vkey(tag) > vkey(cur) else "0"
    try:
        with open(UCONF, "w", encoding="utf-8") as f:
            f.write("version=%s\ntag=%s\nlocal=%s\nhas_update=%s\nsource=%s\n"
                    % (tag, tag, cur, has, src))
    except Exception:
        pass
    progress("check", "100", "本地 %s / 上游 %s" % (cur, tag))
    print("version=%s\ntag=%s\nlocal=%s\nhas_update=%s\nsource=%s" % (tag, tag, cur, has, src))


def download(tag, dest):
    num = tag.lstrip("vV")
    tail = "%s/releases/download/%s/wb2api-panel-%s-linux-arm64.zip" % (REPO, tag, num)
    urls = ["https://github.com/" + tail] + [m + "https://github.com/" + tail for m in mirrors()]
    errs = []
    for i, u in enumerate(urls):
        progress("download", str(10 + i * 15), "下载源 %d/%d" % (i + 1, len(urls)))
        try:
            data = http_get(u, timeout=180)
            if len(data) < 1000000:
                raise RuntimeError("包过小 %d" % len(data))
            with open(dest, "wb") as f:
                f.write(data)
            with zipfile.ZipFile(dest) as z:
                names = z.namelist()
                if "wb2api" not in names and not any(n.endswith("/wb2api") for n in names):
                    raise RuntimeError("包内缺少 wb2api")
                member = "wb2api" if "wb2api" in names else next(n for n in names if n.endswith("/wb2api"))
                raw = z.read(member)
            if not raw.startswith(b"\x7fELF") or len(raw) < 1000000:
                raise RuntimeError("二进制不是有效 arm64 ELF")
            tmp = dest + ".bin"
            with open(tmp, "wb") as f:
                f.write(raw)
            return tmp, u
        except Exception as e:
            errs.append("%s:%s" % (u.split("/")[2], str(e)[:70]))
            if os.path.exists(dest):
                os.remove(dest)
    raise RuntimeError("; ".join(errs))


def update(tag=None):
    cur = read_text(VERSION).strip()
    was_running = bool(pids())
    if not tag:
        tag, _ = latest_tag()
    if tag == cur:
        print("ok=1\nalready=1\nversion=%s" % tag)
        return 0
    progress("apply", "5", "开始更新 %s -> %s" % (cur, tag))
    os.makedirs(BK, exist_ok=True)
    bid = "wb-" + time.strftime("%Y%m%d-%H%M%S")
    bd = os.path.join(BK, bid)
    os.makedirs(bd, exist_ok=True)
    shutil.copy2(PAYLOAD, os.path.join(bd, "wb2api"))
    with open(os.path.join(bd, "VERSION"), "w", encoding="utf-8") as f:
        f.write(cur + "\n")
    progress("apply", "15", "备份完成 %s" % bid)

    zpath = os.path.join(bd, "package.zip")
    newbin, src = download(tag, zpath)
    progress("apply", "55", "包校验通过，替换二进制")

    # 保留模块自带的 prev 机制，供一键回滚
    try:
        shutil.copy2(PAYLOAD, PREV)
    except Exception:
        pass
    tmp = PAYLOAD + ".new"
    shutil.copy2(newbin, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, PAYLOAD)
    with open(VERSION, "w", encoding="utf-8") as f:
        f.write(tag + "\n")
    num = tag.lstrip("vV")
    vc = re.sub(r"[^0-9]", "", num)[:10] or "1"
    txt = read_text(PROP)
    txt = re.sub(r"^version=.*$", "version=" + tag, txt, flags=re.M)
    txt = re.sub(r"^versionCode=.*$", "versionCode=" + vc, txt, flags=re.M)
    with open(PROP, "w", encoding="utf-8") as f:
        f.write(txt)
    progress("apply", "80", "已替换，校验服务状态")

    # 用户要求保持停用：原本停用就绝不拉起服务
    if was_running:
        ok, msg = start()
        if not ok:
            log("重启失败，回滚: %s" % msg)
            shutil.copy2(os.path.join(bd, "wb2api"), PAYLOAD)
            with open(VERSION, "w", encoding="utf-8") as f:
                f.write(cur + "\n")
            print("error=restart_failed_rolled_back")
            return 1
    else:
        stop()  # 确保没有残留进程（例如更新期间被外部拉起）

    progress("apply", "100", "更新完成 %s（保持停用）" % tag)
    log("updated %s -> %s via %s backup=%s" % (cur, tag, src, bid))
    print("ok=1\nversion=%s\nbackup=%s\nrunning=%s" % (tag, bid, "1" if pids() else "0"))
    return 0


def rollback():
    if not os.path.exists(PREV):
        print("error=no_prev_binary")
        return 1
    stop()
    shutil.copy2(PREV, PAYLOAD)
    os.chmod(PAYLOAD, 0o755)
    txt = read_text(PROP)
    cur = read_text(VERSION).strip()
    # prev 没有独立版本号标记，回退一个版本点用备份目录里最近的 VERSION
    bks = sorted([d for d in os.listdir(BK) if d.startswith("wb-")], reverse=True) if os.path.isdir(BK) else []
    old = cur
    for d in bks:
        v = read_text(os.path.join(BK, d, "VERSION")).strip()
        if v and v != cur:
            old = v
            break
    with open(VERSION, "w", encoding="utf-8") as f:
        f.write(old + "\n")
    txt = re.sub(r"^version=.*$", "version=" + old, txt, flags=re.M)
    with open(PROP, "w", encoding="utf-8") as f:
        f.write(txt)
    log("rolled back to %s" % old)
    print("ok=1\nversion=%s" % old)
    return 0


def toggle(action):
    if action == "disable":
        ok = stop()
        try:
            with open(DISABLE, "a"):
                pass
        except Exception:
            pass
        print("ok=1\nrunning=0\nenabled=0")
        return 0 if ok else 1
    if action == "enable":
        try:
            if os.path.exists(DISABLE):
                os.remove(DISABLE)
        except Exception:
            pass
        ok, msg = start()
        log("enable: %s %s" % (ok, msg))
        print("ok=%s\nrunning=%s\nenabled=1\ndetail=%s" % ("1" if ok else "0", "1" if ok else "0", msg[:120]))
        return 0 if ok else 1
    print("error=bad_action")
    return 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "status":
            status()
        elif cmd == "check":
            check()
        elif cmd == "update":
            return update(sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "rollback":
            return rollback()
        elif cmd in ("enable", "disable"):
            return toggle(cmd)
        else:
            print("error=unknown_command")
            return 1
    except Exception as e:
        log("cmd=%s failed: %s" % (cmd, e))
        print("error=%s" % str(e).replace("\n", " ")[:300])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
