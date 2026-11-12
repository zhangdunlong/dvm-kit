#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目更新中心 / Project Update Hub（零依赖，Python 3 标准库）

- 运行在 DroidVM chroot 内，控制平面 /opt/ag2a-console 独立于业务目录。
- 项目由 projects.json 注册表驱动：内置 Agent2API / WorkBuddy2API，
  其余项目用 generic_ctl.py 配置驱动，或用 adapter=custom 接自己的控制器。
- 只监听回环地址，公网由 Caddy 反代提供 HTTPS；所有 API 需 X-Console-Token。
- 自动更新是内置定时器线程，不依赖 crond、不依赖智能体、不依赖面板登录态。
- 历史路径 /ag2a/、目录名 /opt/ag2a-console 保持不变（公网入口与开机自启不改动）。
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 安全边界（2026-11-12 加固）
MAX_JSON_BODY = 1024 * 1024            # 普通 JSON 接口上限 1MB，防止读爆内存
MAX_UPLOAD_BYTES = 120 * 1024 * 1024   # 上传包上限 120MB（与 upload_package 一致）
DOWNLOADS_QUOTA = 500 * 1024 * 1024    # work/downloads 总配额，超出按时间淘汰
SOCKET_TIMEOUT = 60                    # 防慢速连接占用线程

ROOT = os.environ.get("AG2A_CONSOLE_DIR", "/opt/ag2a-console")
WWW = os.path.join(ROOT, "www")
TOKEN_FILE = os.path.join(ROOT, "token")
REG_FILE = os.path.join(ROOT, "projects.json")
REG_DEFAULT = os.path.join(ROOT, "projects.default.json")
UPCONF = os.path.join(ROOT, "upstream.conf")
CONF = os.path.join(ROOT, "auto.conf")
LOG = os.path.join(ROOT, "hub.log")
TASKDIR = os.path.join(ROOT, "tasks")
BIND = os.environ.get("AG2A_CONSOLE_BIND", "127.0.0.1")
PORT = int(os.environ.get("AG2A_CONSOLE_PORT", "3077"))

BRAND = "项目更新中心"
SUBTITLE = "多项目统一更新 · 检查 / 下载 / 备份 / 替换 / 回滚 / 定时"
CAPS = ("check", "update", "rollback", "upload", "auto", "backups", "logs", "toggle")
ADAPTERS = ("agent2api", "wb2api", "generic", "custom")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
CTL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_PREFIXES = ("/opt/", "/app/", "/data/local/", "/usr/local/bin/", "/root/", "/srv/")
FORBIDDEN = ("/", "/system", "/data/adb", "/proc", "/sys", "/dev", "/opt/ag2a-console")

_reg_lock = threading.Lock()
_registry = {}
_pindex = {}
_tasks = {}
_task_locks = {}


# ---------------------------------------------------------------- 基础工具
def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
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


def parse_kv(out):
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def tail(path, n=60):
    return read_text(path).splitlines()[-n:]


def append_log(line, pid=""):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s [hub%s] %s\n" % (stamp, "/" + pid if pid else "", line))
    except Exception:
        pass
    # 项目日志与控制台主日志同步一份，便于沿用旧路径排查
    if pid == "agent2api":
        try:
            with open(os.path.join(ROOT, "update.log"), "a", encoding="utf-8") as f:
                f.write("%s [console] %s\n" % (stamp, line))
        except Exception:
            pass


def pjoin(name):
    """项目相关文件必须在 ROOT 下，禁止路径穿越。"""
    if not name or not CTL_RE.match(os.path.basename(str(name))):
        return ""
    return os.path.join(ROOT, os.path.basename(str(name)))


# ---------------------------------------------------------------- 项目注册表
def default_registry():
    raw = read_text(REG_DEFAULT, "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {"version": 3, "brand": BRAND, "subtitle": SUBTITLE, "projects": []}


def validate_project(p, is_new=False):
    """返回 (ok, msg, 归一化后的 dict)"""
    if not isinstance(p, dict):
        return False, "项目格式不正确", {}
    q = dict(p)
    pid = (q.get("id") or "").strip()
    if not ID_RE.match(pid):
        return False, "项目 ID 需为 2-32 位小写字母/数字/下划线/连字符，且以字母或数字开头", {}
    name = (q.get("name") or "").strip()
    if not name or len(name) > 60:
        return False, "项目名称不能为空且不超过 60 字符", {}
    adapter = (q.get("adapter") or "generic").strip()
    if adapter not in ADAPTERS:
        return False, "未知适配器: %s" % adapter, {}
    ctl = (q.get("ctl") or "").strip()
    if adapter == "generic":
        ctl = "generic_ctl.py"
        q["ctl_type"] = "python3"
    elif not CTL_RE.match(ctl) or not ctl:
        return False, "控制器文件名不合法", {}
    q["id"], q["name"], q["adapter"], q["ctl"] = pid, name, adapter, ctl
    q["ctl_type"] = (q.get("ctl_type") or ("sh" if ctl.endswith(".sh") else "python3")).strip()
    if q["ctl_type"] not in ("sh", "python3", "exec"):
        return False, "ctl_type 只能是 sh / python3 / exec", {}
    if not pjoin(ctl) or not os.path.isfile(pjoin(ctl)):
        return False, "控制器文件不存在: %s" % ctl, {}
    caps = q.get("capabilities") or ["check", "update", "rollback", "logs"]
    if not isinstance(caps, list) or any(x not in CAPS for x in caps):
        return False, "capabilities 含不支持的能力", {}
    q["capabilities"] = caps
    q["fields"] = q.get("fields") or [
        {"key": "version", "label": "当前版本", "fmt": "text", "big": True},
        {"key": "latest", "label": "最新版本", "fmt": "text"},
        {"key": "has_update", "label": "更新判定", "fmt": "update"},
        {"key": "binary_size", "label": "文件大小", "fmt": "size"},
    ]
    q["latest_fields"] = q.get("latest_fields") or [
        {"key": "version", "label": "最新版本", "fmt": "text", "big": True},
        {"key": "has_update", "label": "更新判定", "fmt": "update"},
    ]
    q["latest_file"] = q.get("latest_file") or ("latest" if adapter == "agent2api" else pid + "-latest")
    q["log_file"] = q.get("log_file") or ("update.log" if adapter == "agent2api" else pid + ".log")
    q["progress_file"] = q.get("progress_file") or ("progress" if adapter == "agent2api" else "progress." + pid)
    q["icon"] = (q.get("icon") or name[:2]).strip()[:4]
    q["active"] = bool(q.get("active", True))
    q["builtin"] = bool(q.get("builtin", False))
    q.setdefault("desc", "")
    q.setdefault("notes", "")
    q.setdefault("home", "")
    q.setdefault("repo", "")
    if adapter in ("generic", "custom"):
        t = (q.get("target") or "").strip()
        # 直接用原始 POSIX 字符串判断，避免 Windows 上 normpath 把 / 转成 \ 导致校验失效
        if adapter == "generic" and t and t.startswith("/"):
            if t.startswith("/opt/ag2a-console"):
                return False, "target 不允许指向控制台自身", {}
            if not t.startswith(SAFE_PREFIXES):
                return False, "target 必须在 /opt /app /data/local /usr/local/bin /root /srv 下", {}
            if any(t == bad or t.startswith(bad + "/") for bad in FORBIDDEN):
                return False, "target 位于禁止目录", {}
        for k in ("stop_cmd", "start_cmd"):
            v = (q.get(k) or "").strip()
            if "\n" in v or len(v) > 500 or "rm -rf" in v.lower():
                return False, "%s 含不安全内容" % k, {}
            q[k] = v
    if is_new and pid in _pindex:
        return False, "项目 ID 已存在: %s" % pid, {}
    return True, "", q


def save_registry(reg):
    tmp = REG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REG_FILE)
    load_registry()


def load_registry():
    global _registry, _pindex, BRAND, SUBTITLE
    if not os.path.exists(REG_FILE) and os.path.isfile(REG_DEFAULT):
        try:
            shutil.copyfile(REG_DEFAULT, REG_FILE)
        except Exception:
            pass
    try:
        reg = json.loads(read_text(REG_FILE, "{}"))
        if not isinstance(reg.get("projects"), list) or not reg["projects"]:
            raise ValueError("empty")
    except Exception:
        reg = default_registry()
    with _reg_lock:
        _registry = reg
        _pindex = {p.get("id"): p for p in reg["projects"] if isinstance(p, dict) and p.get("id")}
        BRAND = reg.get("brand") or BRAND
        SUBTITLE = reg.get("subtitle") or SUBTITLE


def project(pid):
    return _pindex.get(pid)


def default_pid():
    for p in _registry.get("projects", []):
        if p.get("active", True):
            return p["id"]
    return next(iter(_pindex), "")


def public_project(p):
    return dict(p)


# ---------------------------------------------------------------- 控制器调用
def py_bin():
    return "/usr/bin/python3" if os.path.isfile("/usr/bin/python3") else sys.executable


def ctl_path(p):
    return pjoin(p.get("ctl"))


def ctl_invoke(p, *args, timeout=900):
    path = ctl_path(p)
    if not path:
        return 1, "error=bad_controller"
    t = p.get("ctl_type", "sh")
    if t == "python3":
        cmd = [py_bin(), path]
    elif t == "exec":
        cmd = [path]
    else:
        cmd = ["sh", path]
    cmd += [str(a) for a in args]
    env = os.environ.copy()
    env["PUH_PROJECT"] = p["id"]
    env["PUH_CONSOLE_DIR"] = ROOT
    env["AG2A_CONSOLE_DIR"] = ROOT
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=env)
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except Exception as e:
        return 1, str(e)


def ctl(p, *args, **kw):
    return ctl_invoke(p, *args, **kw)


def latest_path(p):
    return pjoin(p.get("latest_file"))


def log_path(p):
    return pjoin(p.get("log_file"))


def progress_path(p):
    return pjoin(p.get("progress_file"))


# ---------------------------------------------------------------- 任务状态
def task_lock(pid):
    with _reg_lock:
        if pid not in _task_locks:
            _task_locks[pid] = threading.Lock()
        return _task_locks[pid]


def task_of(pid):
    if pid not in _tasks:
        f = os.path.join(TASKDIR, pid + ".json")
        try:
            t = json.loads(read_text(f, "{}"))
            if not isinstance(t, dict):
                raise ValueError()
        except Exception:
            t = {}
        _tasks[pid] = {"running": False, "kind": "", "started": "", "result": "",
                       "error": "", **{k: v for k, v in t.items() if k != "running"}}
    return _tasks[pid]


def set_task(pid, **kw):
    t = task_of(pid)
    t.update(kw)
    try:
        os.makedirs(TASKDIR, exist_ok=True)
        tmp = os.path.join(TASKDIR, pid + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(TASKDIR, pid + ".json"))
    except Exception:
        pass


# ---------------------------------------------------------------- 上游配置
def up_conf():
    c = read_kv(UPCONF)
    return {
        "PROXY": c.get("PROXY") or "http://127.0.0.1:7890",
        "MIRRORS": c.get("MIRRORS") or "https://gh-proxy.com/https://github.com/;https://ghfast.top/https://github.com/",
    }


def set_up_conf(proxy, mirrors):
    proxy = (proxy or "").strip()
    mirrors = (mirrors or "").strip()
    if proxy and not re.match(r"^https?://[A-Za-z0-9._:\-\[\]]+(:\d+)?/?$", proxy):
        return False, "代理地址格式不正确"
    for m in [x.strip() for x in mirrors.split(";") if x.strip()]:
        if not re.match(r"^https://[A-Za-z0-9.\-]+/", m):
            return False, "镜像源格式不正确: " + m[:60]
    tmp = UPCONF + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("PROXY=%s\nMIRRORS=%s\n" % (proxy, mirrors))
        os.replace(tmp, UPCONF)
        append_log("上游配置已更新 proxy=%s mirrors=%s" % (proxy, mirrors[:80]))
        return True, "已保存"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- 状态聚合
def parse_progress(raw):
    pr = (raw or "").strip().split("|")
    return {"step": pr[0], "percent": pr[1], "message": pr[2] if len(pr) > 2 else ""} \
        if len(pr) >= 3 else {}


def project_status(pid, timeout=30):
    p = project(pid)
    if not p:
        return {"ok": False, "error": "项目不存在"}
    code, out = ctl_invoke(p, "status", timeout=timeout)
    d = parse_kv(out)
    d["ok"] = code == 0
    d["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d["project"] = public_project(p)
    d["progress"] = parse_progress(d.pop("progress", "") or read_text(progress_path(p)))
    d["latest"] = read_kv(latest_path(p))
    if p.get("state_file"):
        d["state"] = read_kv(pjoin(p.get("state_file")))
    d["auto"] = read_kv(CONF)
    d["task"] = dict(task_of(pid))
    d["logs"] = tail(log_path(p), 60)
    d["upstream_conf"] = up_conf()
    d["brand"] = BRAND
    d["subtitle"] = SUBTITLE
    if "backups" in p.get("capabilities", []):
        _, bl = ctl_invoke(p, "backups", timeout=20)
        d["backups"] = [x.strip() for x in bl.splitlines() if x.strip().startswith("bk-")]
    else:
        bkdir = os.path.join(ROOT, "work", "backups")
        pref = pid + "-"
        d["backups"] = sorted([x for x in os.listdir(bkdir)
                               if x.startswith(pref)], reverse=True)[:12] \
            if os.path.isdir(bkdir) else []
    return d


def project_summary(pid):
    try:
        d = project_status(pid, timeout=12)
        return {"ok": d.get("ok", False),
                "version": d.get("version", ""), "latest": d.get("latest", {}).get("version", ""),
                "has_update": d.get("latest", {}).get("has_update", d.get("has_update", "0")),
                "running": d.get("running", ""), "enabled": d.get("enabled", ""),
                "error": d.get("error", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def projects_payload():
    items = []
    for p in _registry.get("projects", []):
        q = public_project(p)
        q["summary"] = project_summary(p["id"]) if p.get("active", True) else {}
        items.append(q)
    return {"ok": True, "service": "project-update-hub", "brand": BRAND,
            "subtitle": SUBTITLE, "version": _registry.get("version", 3),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "projects": items, "config": up_conf(), "auto": read_kv(CONF)}


# ---------------------------------------------------------------- 任务执行
def start_task(pid, kind, version=None, backup_id=None):
    if not project(pid):
        return False, "项目不存在"
    lock = task_lock(pid)
    with lock:
        if task_of(pid)["running"]:
            return False, "任务正在运行"
        set_task(pid, running=True, kind=kind,
                 started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 result="", error="")
    threading.Thread(target=run_task, args=(pid, kind, version, backup_id), daemon=True).start()
    return True, ""


def run_task(pid, kind, version=None, backup_id=None):
    p = project(pid)
    if not p:
        set_task(pid, running=False, result="failed", error="项目不存在")
        return
    ok, err, ver = False, "", ""
    no_update = False
    try:
        append_log("开始任务 %s/%s" % (pid, kind), pid)
        if kind == "rollback":
            args = [backup_id] if (backup_id and p["adapter"] == "agent2api") else []
            code, out = ctl_invoke(p, "rollback", *args, timeout=600)
            ok = code == 0 and "error=" not in out
            err = "" if ok else out[-400:]
        elif kind in ("enable", "disable"):
            code, out = ctl_invoke(p, kind, timeout=120)
            ok = code == 0 and "error=" not in out
            err = "" if ok else out[-400:]
        else:
            latest = read_kv(latest_path(p))
            offline = p["adapter"] != "agent2api" and latest.get("channel") == "upload"
            if p["adapter"] == "agent2api" or (not version and not offline):
                code, out = ctl_invoke(p, "check", timeout=150)
                if code != 0 or "error=" in out:
                    raise RuntimeError("检查更新失败: " + out[-300:])
                latest = read_kv(latest_path(p))
                if (p["adapter"] == "agent2api" and not version
                        and latest.get("has_update") != "1"):
                    append_log("当前已是最新版本 %s" % latest.get("version"), pid)
                    no_update = True
                    set_task(pid, result="no_update")
                    return
            ver = version or latest.get("version") or ""
            if p["adapter"] == "agent2api":
                append_log("开始下载 %s" % ver, pid)
                code, out = ctl_invoke(p, "download", ver, timeout=900)
                pkg_line = [x for x in out.splitlines() if x.startswith("pkg=")]
                if code != 0 or not pkg_line:
                    raise RuntimeError("下载失败: " + out[-300:])
                pkg = pkg_line[-1][4:].strip()
                append_log("开始应用 %s" % pkg, pid)
                code, out = ctl_invoke(p, "apply", pkg, timeout=900)
                ok = code == 0 and "ok=1" in out
                err = "" if ok else "应用失败（已自动回滚）: " + out[-400:]
            else:
                code, out = ctl_invoke(p, "update", ver, timeout=900)
                ok = code == 0 and "error=" not in out
                err = "" if ok else out[-400:]
                if ok and offline:
                    # 离线包更新后把本地清单归零，避免重复提示
                    f = latest_path(p)
                    kv = read_kv(f)
                    kv["has_update"] = "0"
                    kv["local"] = ver
                    kv["channel"] = "local"
                    try:
                        with open(f, "w", encoding="utf-8") as h:
                            for k, v in kv.items():
                                h.write("%s=%s\n" % (k, v))
                    except Exception:
                        pass
        if ok:
            append_log("任务完成 %s/%s %s" % (pid, kind, ver), pid)
        else:
            append_log("任务失败 %s/%s %s" % (pid, kind, err[-200:]), pid)
    except Exception as e:
        ok, err = False, str(e)
        append_log("任务异常 %s/%s: %s" % (pid, kind, e), pid)
    finally:
        if no_update:
            set_task(pid, running=False)
        else:
            set_task(pid, running=False, result="ok" if ok else "failed",
                     error="" if ok else err[-400:])


def auto_conf():
    c = read_kv(CONF)
    return c.get("AUTO", "off"), c.get("AT", "04:00"), c.get("CHANNEL", "beta")


def auto_cmd(cmd, arg=None):
    p = project("agent2api")
    if not p:
        return 1, "缺少默认控制器"
    if cmd in ("on", "off"):
        return ctl_invoke(p, "auto", cmd, timeout=30)
    if ":" in cmd:
        return ctl_invoke(p, "auto", cmd, timeout=30)
    if cmd in ("beta", "stable"):
        return ctl_invoke(p, "auto", "channel", cmd, timeout=30)
    return 0, ""


def auto_loop():
    """每天到点，为所有「启用且参与自动更新」的项目依次触发更新。"""
    last_done = ""
    while True:
        try:
            on, at, channel = auto_conf()
            now = datetime.now()
            if on == "on" and now.strftime("%H:%M") == at:
                stamp = now.strftime("%Y-%m-%d")
                if last_done != stamp:
                    last_done = stamp
                    for p in _registry.get("projects", []):
                        if not p.get("active", True) or "auto" not in p.get("capabilities", []):
                            continue
                        if task_of(p["id"])["running"]:
                            continue
                        append_log("自动更新触发（%s，通道 %s）" % (at, channel), p["id"])
                        start_task(p["id"], "update")
        except Exception as e:
            append_log("自动更新线程异常: %s" % e)
        time.sleep(60)


# ---------------------------------------------------------------- 离线上传
def prune_downloads(dl, keep_path, quota=DOWNLOADS_QUOTA):
    """上传包缓存按总量淘汰：防止反复上传不同版本把磁盘写满。"""
    try:
        items = []
        total = 0
        for name in os.listdir(dl):
            fp = os.path.join(dl, name)
            if not os.path.isfile(fp):
                continue
            try:
                sz = os.path.getsize(fp)
                items.append((os.path.getmtime(fp), fp, sz))
                total += sz
            except Exception:
                continue
        if total <= quota:
            return 0
        removed = 0
        for _mt, fp, sz in sorted(items):
            if fp == keep_path:
                continue
            try:
                os.remove(fp)
                total -= sz
                removed += 1
            except Exception:
                continue
            if total <= quota:
                break
        return removed
    except Exception:
        return 0


def upload_package(self, pid, tag, size, expect_sha):
    p = project(pid)
    if not p or "upload" not in p.get("capabilities", []):
        return 400, {"ok": False, "error": "该项目不支持离线上传"}
    tag = (tag or "").strip()
    if not re.match(r"^v?\d[\w.\-]*$", tag) or len(tag) > 48:
        return 400, {"ok": False, "error": "版本号格式不正确"}
    if not tag.startswith("v"):
        tag = "v" + tag
    if size < 1000000 or size > MAX_UPLOAD_BYTES:
        return 400, {"ok": False, "error": "包体积异常（需在 1MB~120MB）"}
    if pid == "agent2api":
        dl = os.path.join(ROOT, "work", "downloads")
        ext, asset = ".tar.gz", "agent2api-server-linux-arm64.tar.gz"
        latest = pjoin("latest")
        local = read_kv(os.path.join(ROOT, "work", "version")).get("version", "")
    else:
        dl = os.path.join(ROOT, "work", "downloads", pid)
        pk = (p.get("package") or "raw").lower()
        ext = {"tar.gz": ".tar.gz", "tgz": ".tar.gz", "zip": ".zip"}.get(pk, ".bin")
        asset = (p.get("asset") or "package").replace("{tag}", tag).replace("{ver}", tag[1:])
        latest = latest_path(p)
        local = project_status(pid, timeout=15).get("version", "")
    os.makedirs(dl, exist_ok=True)
    path = os.path.join(dl, tag + ext)
    tmp = path + ".upload"
    h = hashlib.sha256()
    left = size
    try:
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                left -= len(chunk)
        if left > 0:
            raise IOError("上传中断，实际接收 %d/%d 字节" % (size - left, size))
        digest = h.hexdigest()
        if expect_sha and expect_sha.lower() != digest:
            raise ValueError("sha256 不匹配：%s" % digest)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)   # 上传包仅 root 可读
        except Exception:
            pass
        pruned = prune_downloads(dl, path)
        if pruned:
            append_log("上传缓存超配额，已淘汰 %d 个旧包" % pruned, pid)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return 400, {"ok": False, "error": str(e)}
    with open(os.path.join(dl, tag + ".sha256"), "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (digest, asset))
    kv = {"version": tag, "tag": tag, "published": "local-upload", "prerelease": "0",
          "assetUrl": "local://" + tag, "shaUrl": "local://" + tag + ".sha256",
          "assetName": asset, "has_update": "1", "local": local, "channel": "upload",
          "notes": "面板上传的官方包", "source": "local-upload"}
    with open(latest, "w", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write("%s=%s\n" % (k, v))
    append_log("面板上传完成 %s/%s，%d 字节，sha256=%s" % (pid, tag, size, digest), pid)
    return 200, {"ok": True, "project": pid, "version": tag, "size": size, "sha256": digest}


# ---------------------------------------------------------------- 项目管理
def manage_projects(body):
    action = (body.get("action") or "").strip()
    pid = (body.get("id") or "").strip()
    reg = dict(_registry)
    items = [dict(p) for p in reg.get("projects", [])]
    if action in ("add", "update"):
        raw = body.get("project")
        if not isinstance(raw, dict):
            return 400, {"ok": False, "message": "缺少项目配置"}
        existing = next((x for x in items if x.get("id") == raw.get("id")), None)
        if action == "add" and existing:
            return 400, {"ok": False, "message": "项目 ID 已存在"}
        if action == "update" and not existing:
            return 400, {"ok": False, "message": "项目不存在"}
        if existing and existing.get("builtin"):
            # 内置项目只允许改展示字段与开关，控制器/映射文件不可改
            allowed = ("name", "desc", "icon", "home", "notes", "active",
                       "capabilities", "fields", "latest_fields")
            merged = dict(existing)
            merged.update({k: v for k, v in raw.items() if k in allowed})
            merged["id"] = existing["id"]
            for k in ("adapter", "ctl", "ctl_type", "latest_file", "log_file",
                      "progress_file", "state_file", "repo", "builtin"):
                merged[k] = existing.get(k)
            raw = merged
        ok, msg, q = validate_project(raw, is_new=(action == "add"))
        if not ok:
            return 400, {"ok": False, "message": msg}
        if existing:
            items = [q if x.get("id") == q["id"] else x for x in items]
        else:
            items.append(q)
        reg["projects"] = items
    elif action == "remove":
        hit = next((x for x in items if x.get("id") == pid), None)
        if not hit:
            return 400, {"ok": False, "message": "项目不存在"}
        if hit.get("builtin"):
            return 400, {"ok": False, "message": "内置项目不可删除，可改为停用"}
        items = [x for x in items if x.get("id") != pid]
        reg["projects"] = items
    elif action in ("activate", "deactivate"):
        hit = next((x for x in items if x.get("id") == pid), None)
        if not hit:
            return 400, {"ok": False, "message": "项目不存在"}
        hit["active"] = action == "activate"
        reg["projects"] = items
    elif action == "reset":
        reg = default_registry()
    elif action in ("up", "down"):
        i = next((k for k, x in enumerate(items) if x.get("id") == pid), -1)
        j = i - 1 if action == "up" else i + 1
        if i < 0 or j < 0 or j >= len(items):
            return 400, {"ok": False, "message": "无法移动"}
        items[i], items[j] = items[j], items[i]
        reg["projects"] = items
    else:
        return 400, {"ok": False, "message": "未知操作"}
    try:
        save_registry(reg)
        append_log("项目注册表变更 action=%s id=%s" % (action, pid))
        payload = projects_payload()
        payload["message"] = "已保存"
        return 200, payload
    except Exception as e:
        return 400, {"ok": False, "message": str(e)}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    # 防慢速连接：socket 读超时，避免单连接长期占用线程
    timeout = SOCKET_TIMEOUT
    server_version = "ProjectUpdateHub/3.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _token_ok(self):
        want = read_text(TOKEN_FILE).strip()
        got = (self.headers.get("X-Console-Token") or "").strip()
        # 只接受 Header：URL 里的 token 会进入访问日志 / 浏览器历史 / Referer
        if not want or not got:
            return False
        return hmac.compare_digest(got, want)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        if n > MAX_JSON_BODY:
            # 不读入内存；由调用方根据 _body_oversize 返回 413
            self._body_oversize = True
            self.close_connection = True
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html", "/ag2a/", "/ag2a", "/hub/", "/hub"):
            try:
                with open(os.path.join(WWW, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                return self._send(404, {"error": "index.html missing"})
        if not self._token_ok():
            return self._send(401, {"error": "unauthorized"})
        if p == "/api/projects":
            return self._send(200, projects_payload())
        m = re.match(r"^/api/p/([a-z0-9_-]+)/([a-z]+)$", p)
        if m:
            pid, act = m.group(1), m.group(2)
            if not project(pid):
                return self._send(404, {"error": "项目不存在"})
            if act == "status":
                return self._send(200, project_status(pid))
            if act == "logs":
                n = 120
                if "n=" in self.path:
                    try:
                        n = min(500, max(10, int(self.path.split("n=", 1)[1].split("&", 1)[0])))
                    except Exception:
                        pass
                return self._send(200, {"lines": tail(log_path(project(pid)), n)})
            return self._send(404, {"error": "not found"})
        # ---- 旧 API 兼容（映射到默认项目）
        if p == "/api/status":
            return self._send(200, project_status("agent2api"))
        if p == "/api/wb/status":
            return self._send(200, project_status("wb2api"))
        if p == "/api/config":
            return self._send(200, {"ok": True, "config": up_conf()})
        if p == "/api/logs":
            return self._send(200, {"lines": tail(os.path.join(ROOT, "update.log"), 120)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._token_ok():
            return self._send(401, {"error": "unauthorized"})
        p = self.path.split("?", 1)[0]
        # 二进制上传必须在 _body() 之前处理
        up = re.match(r"^/api/p/([a-z0-9_-]+)/upload$", p)
        if up or p == "/api/upload":
            pid = up.group(1) if up else "agent2api"
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                n = 0
            code, payload = upload_package(
                self, pid, self.headers.get("X-Console-Version", ""), n,
                self.headers.get("X-Console-Sha256", ""))
            return self._send(code, payload)

        b = self._body()
        if getattr(self, "_body_oversize", False):
            return self._send(413, {"ok": False, "error": "请求体过大（JSON 上限 1MB）"})
        # ---- 项目管理
        if p == "/api/projects":
            code, payload = manage_projects(b)
            return self._send(code, payload)
        # ---- 通用项目 API
        m = re.match(r"^/api/p/([a-z0-9_-]+)/([a-z]+)$", p)
        if m:
            pid, act = m.group(1), m.group(2)
            pr = project(pid)
            if not pr:
                return self._send(404, {"error": "项目不存在"})
            if act == "check":
                code, out = ctl_invoke(pr, "check", timeout=150)
                return self._send(200, {"ok": code == 0, "output": out,
                                        "latest": read_kv(latest_path(pr))})
            if act == "update":
                ok, msg = start_task(pid, "update", b.get("version") or None)
                return self._send(202 if ok else 409,
                                  {"started": ok, "task": dict(task_of(pid)), "error": msg})
            if act == "rollback":
                ok, msg = start_task(pid, "rollback", None, b.get("backup") or None)
                return self._send(202 if ok else 409,
                                  {"started": ok, "task": dict(task_of(pid)), "error": msg})
            if act == "toggle":
                a = b.get("action", "")
                if a not in ("enable", "disable") or "toggle" not in pr.get("capabilities", []):
                    return self._send(400, {"error": "该项目不支持启停，或 action 不合法"})
                ok, msg = start_task(pid, a)
                return self._send(202 if ok else 409,
                                  {"started": ok, "task": dict(task_of(pid)), "error": msg})
            if act == "auto":
                code, out = auto_cmd(b.get("cmd", "status"))
                return self._send(200, {"ok": code == 0, "output": out, "auto": read_kv(CONF)})
            return self._send(404, {"error": "not found"})
        # ---- 旧 API 兼容
        if p == "/api/check":
            code, out = ctl_invoke(project("agent2api"), "check", timeout=150)
            return self._send(200, {"ok": code == 0, "output": out,
                                    "latest": read_kv(latest_path(project("agent2api")))})
        if p == "/api/apply":
            ok, msg = start_task("agent2api", "update", b.get("version") or None)
            return self._send(202 if ok else 409,
                              {"started": ok, "task": dict(task_of("agent2api")), "error": msg})
        if p == "/api/rollback":
            ok, msg = start_task("agent2api", "rollback", None, b.get("backup") or None)
            return self._send(202 if ok else 409,
                              {"started": ok, "task": dict(task_of("agent2api")), "error": msg})
        if p == "/api/wb/check":
            code, out = ctl_invoke(project("wb2api"), "check", timeout=150)
            return self._send(200, {"ok": code == 0, "output": out,
                                    "latest": read_kv(latest_path(project("wb2api")))})
        if p == "/api/wb/update":
            ok, msg = start_task("wb2api", "update", b.get("version") or None)
            return self._send(202 if ok else 409,
                              {"started": ok, "task": dict(task_of("wb2api")), "error": msg})
        if p == "/api/wb/rollback":
            ok, msg = start_task("wb2api", "rollback")
            return self._send(202 if ok else 409,
                              {"started": ok, "task": dict(task_of("wb2api")), "error": msg})
        if p == "/api/wb/toggle":
            a = b.get("action", "")
            if a not in ("enable", "disable"):
                return self._send(400, {"error": "action 必须是 enable 或 disable"})
            ok, msg = start_task("wb2api", a)
            return self._send(202 if ok else 409,
                              {"started": ok, "task": dict(task_of("wb2api")), "error": msg})
        if p == "/api/config":
            ok, msg = set_up_conf(b.get("PROXY", ""), b.get("MIRRORS", ""))
            return self._send(200 if ok else 400, {"ok": ok, "message": msg, "config": up_conf()})
        if p == "/api/auto":
            code, out = auto_cmd(b.get("cmd", "status"))
            return self._send(200, {"ok": code == 0, "output": out, "auto": read_kv(CONF)})
        return self._send(404, {"error": "not found"})


def main():
    os.makedirs(ROOT, exist_ok=True)
    os.makedirs(TASKDIR, exist_ok=True)
    load_registry()
    if not os.path.exists(CONF):
        with open(CONF, "w", encoding="utf-8") as f:
            f.write("AUTO=off\nAT=04:00\nCHANNEL=beta\n")
    threading.Thread(target=auto_loop, daemon=True).start()
    append_log("项目更新中心启动 %s:%s，已加载 %d 个项目" % (BIND, PORT, len(_pindex)))
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
