#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用项目更新控制器 · 配置驱动（项目更新中心 / Project Update Hub）

一个控制器服务所有「自定义项目」：项目参数全部来自 /opt/ag2a-console/projects.json
中 adapter=generic 的条目，通过环境变量 PUH_PROJECT 指定当前项目。

命令协议（与内置项目一致，输出 key=value）：
    status                 当前版本 / 上游 / 运行状态 / 进度
    check [beta|stable]    查 GitHub releases.atom，写 <id>-latest
    update [tag]           备份 → 下载(或本地上传包) → 校验 → 原子替换 → 重启 → 探活 → 失败回滚
    rollback               恢复 target.prev 或最近备份
    enable / disable       服务启停（disable 标记落盘，更新时绝不偷偷拉起）

可直接被面板调用，也可单独在 chroot 内执行：
    PUH_PROJECT=myapp python3 /opt/ag2a-console/generic_ctl.py status

安全约束：
- target 只允许绝对路径且位于白名单前缀下；相对路径按控制台目录解析（便于测试）。
- 任何替换都先备份；health_url 探活失败自动回滚。
- stop_cmd / start_cmd 由项目配置提供，禁止换行与 rm -rf。
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

CONSOLE = os.environ.get("PUH_CONSOLE_DIR") or os.environ.get("AG2A_CONSOLE_DIR") or "/opt/ag2a-console"
PID = os.environ.get("PUH_PROJECT", "").strip()
REG = os.path.join(CONSOLE, "projects.json")
WORK = os.path.join(CONSOLE, "work")
BK = os.path.join(WORK, "backups")
DL = os.path.join(WORK, "downloads")
VERDIR = os.path.join(WORK, "versions")

SAFE_PREFIXES = ("/opt/", "/app/", "/data/local/", "/usr/local/bin/", "/root/", "/srv/")
FORBIDDEN = ("/", "/system", "/data/adb", "/proc", "/sys", "/dev", "/opt/ag2a-console")

# 解包单文件上限：上传包本身 ≤120MB，解压后正常不会超过 200MB。
# 超过即视为压缩炸弹，直接拒绝，避免 read() 把内存吃穿。
MAX_MEMBER_BYTES = 200 * 1024 * 1024

sys.path.insert(0, CONSOLE)
try:
    import upstream as UP
except Exception:
    UP = None


def log(msg):
    p = os.path.join(CONSOLE, "%s.log" % PID)
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def progress(step, pct, msg):
    p = os.path.join(CONSOLE, "progress.%s" % PID)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("%s|%s|%s" % (step, pct, msg))
    except Exception:
        pass


def read_text(p, default=""):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def read_kv(p):
    out = {}
    for line in read_text(p).splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_kv(p, kv):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write("%s=%s\n" % (k, v))
    os.replace(tmp, p)


def cfg():
    if not PID:
        raise RuntimeError("缺少环境变量 PUH_PROJECT")
    try:
        reg = json.loads(read_text(REG, "{}"))
    except Exception as e:
        raise RuntimeError("projects.json 无法解析: %s" % e)
    hit = [p for p in reg.get("projects", []) if p.get("id") == PID]
    if not hit:
        raise RuntimeError("项目中不存在: %s" % PID)
    return hit[0]


def vkey(v):
    """通用版本比较：数字段补齐 6 位，预发布权重低于正式版。"""
    s = (v or "").strip().lstrip("vV")
    pre = bool(re.search(r"-(alpha|beta|rc|pre|dev|nightly|snapshot)", s, re.I))
    nums = [int(x) for x in re.findall(r"\d+", s)][:6]
    nums += [0] * (6 - len(nums))
    return tuple(nums) + (0 if pre else 1,)


def target_path(c):
    raw = (c.get("target") or "").strip()
    if not raw:
        raise RuntimeError("项目未配置 target（目标文件路径）")
    # 相对路径按控制台目录解析（沙盒内，供自定义小项目 / 测试使用）
    t = os.path.normpath(os.path.join(CONSOLE, raw)) if not raw.startswith("/") else os.path.normpath(raw)
    if raw.startswith("/"):
        if t.startswith("/opt/ag2a-console"):
            raise RuntimeError("target 不允许指向控制台自身: %s" % t)
        if not t.startswith(SAFE_PREFIXES):
            raise RuntimeError("target 不在允许的前缀内: %s" % t)
        for bad in FORBIDDEN:
            if t == bad or t.startswith(bad + "/"):
                raise RuntimeError("target 位于禁止目录: %s" % t)
    if os.path.isdir(t):
        raise RuntimeError("target 必须是文件而不是目录: %s" % t)
    return t


def ext_of(c):
    return {"tar.gz": ".tar.gz", "tgz": ".tar.gz", "zip": ".zip",
            "raw": ".bin", "bin": ".bin"}.get((c.get("package") or "raw").lower(), ".bin")


def ver_path(c):
    v = (c.get("version_file") or "").strip()
    if not v:
        return os.path.join(VERDIR, PID)
    return v if os.path.isabs(v) else os.path.join(CONSOLE, v)


def disabled_file():
    return os.path.join(WORK, "state", PID + ".disabled")


def local_version(c):
    cmd = (c.get("version_cmd") or "").strip()
    if cmd:
        try:
            out = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=20)
            v = (out.stdout or b"").decode("utf-8", "replace").strip().splitlines()
            if v:
                return v[0].strip()
        except Exception:
            pass
    return read_text(ver_path(c)).strip().splitlines()[0].strip() if read_text(ver_path(c)).strip() else ""


def set_version(c, v):
    p = ver_path(c)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(v + "\n")
    os.replace(tmp, p)


def is_running(c):
    url = (c.get("health_url") or "").strip()
    if url:
        try:
            import urllib.request
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(urllib.request.Request(url, headers={"User-Agent": "puh/3.0"}), timeout=4) as r:
                return "1" if getattr(r, "status", 200) < 400 else "0"
        except Exception:
            return "0"
    cmd = (c.get("health_cmd") or "").strip()
    if cmd:
        ok, _ = run_shell(cmd, timeout=15)
        return "1" if ok else "0"
    pat = (c.get("pid_pattern") or "").strip()
    if pat:
        for n in os.listdir("/proc") if os.path.isdir("/proc") else []:
            if not n.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % n, "rb") as f:
                    if pat.encode() in f.read():
                        return "1"
            except Exception:
                pass
    return "0"


def run_shell(cmd, timeout=60):
    cmd = (cmd or "").strip()
    if not cmd:
        return True, ""
    if "\n" in cmd or len(cmd) > 500 or "rm -rf" in cmd.lower():
        raise RuntimeError("stop/start 命令包含不安全内容")
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode == 0, (p.stdout or b"").decode("utf-8", "replace")[-200:]
    except Exception as e:
        return False, str(e)


def health_ok(c, tries=12):
    cmd = (c.get("health_cmd") or "").strip()
    if cmd:
        for _ in range(tries):
            ok, _msg = run_shell(cmd, timeout=15)
            if ok:
                return True
            time.sleep(2)
        return False
    url = (c.get("health_url") or "").strip()
    if not url:
        return True
    import urllib.request
    for _ in range(tries):
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(urllib.request.Request(url, headers={"User-Agent": "puh/3.0"}), timeout=4) as r:
                if getattr(r, "status", 200) < 400:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def atom_urls(c):
    repo = (c.get("repo") or "").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repo):
        raise RuntimeError("repo 格式应为 owner/name: %s" % repo)
    atom = "https://github.com/%s/releases.atom" % repo
    px, mirrors = UP.conf() if UP else ("http://127.0.0.1:7890", [])
    urls = [atom] + [m + atom for m in mirrors]
    urls.append("https://api.github.com/repos/%s/releases?per_page=15" % repo)
    return urls, px


def fetch_releases(c, channel="beta"):
    urls, px = atom_urls(c)
    errs = []
    for u in urls:
        try:
            data = UP.http_get(u, px, timeout=25) if UP else b""
            if b"<feed" in data[:400] or b"<entry" in data[:2000]:
                rel = UP.parse_atom(data)
                if rel:
                    return rel, u
            j = json.loads(data.decode("utf-8", "replace"))
            if isinstance(j, list) and j:
                return j, u
        except Exception as e:
            errs.append("%s:%s" % (u.split("/")[2], str(e)[:60]))
    raise RuntimeError("上游全部不可达: " + "; ".join(errs))


def pick_release(rel, channel):
    for r in rel:
        if r.get("draft"):
            continue
        if channel == "stable" and r.get("prerelease"):
            continue
        return r
    return None


def asset_name(c, tag):
    return (c.get("asset") or "").replace("{tag}", tag) \
        .replace("{ver}", tag.lstrip("vV")).replace("{arch}", "arm64")


def download_package(c, tag):
    """优先用面板离线上传的包；否则按 官方 → 镜像 顺序下载。"""
    ext = ext_of(c)
    os.makedirs(os.path.join(DL, PID), exist_ok=True)
    local = os.path.join(DL, PID, tag + ext)
    if os.path.isfile(local) and os.path.getsize(local) > 1000:
        return open(local, "rb").read(), "local-upload"
    repo = (c.get("repo") or "").strip().strip("/")
    name = asset_name(c, tag)
    if not name:
        raise RuntimeError("项目未配置 asset（release 资产名）")
    tail = "https://github.com/%s/releases/download/%s/%s" % (repo, tag, name)
    _, mirrors = UP.conf() if UP else ("", [])
    urls = [tail] + [m + tail for m in mirrors]
    px, _ = UP.conf() if UP else ("http://127.0.0.1:7890", [])
    errs = []
    for i, u in enumerate(urls):
        progress("download", str(10 + i * 20), "下载源 %d/%d" % (i + 1, len(urls)))
        try:
            data = UP.http_get(u, px, timeout=180) if UP else b""
            if len(data) < int(c.get("min_size") or 1000):
                raise RuntimeError("包过小 %d" % len(data))
            with open(local + ".tmp", "wb") as f:
                f.write(data)
            os.replace(local + ".tmp", local)
            return data, u
        except Exception as e:
            errs.append("%s:%s" % (u.split("/")[2], str(e)[:70]))
    raise RuntimeError("下载失败: " + "; ".join(errs))


def extract(c, data):
    """从包里取出要写入 target 的字节。"""
    kind = (c.get("package") or "raw").lower()
    member = (c.get("member") or "").strip()
    if kind == "raw" or kind == "bin":
        return data
    if kind in ("tar.gz", "tgz"):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            names = [n for n in t.getnames() if not n.endswith("/")]
            pick = member if member and member in names else None
            if not pick and member:
                pick = next((n for n in names if n.endswith("/" + member) or n == member), None)
            if not pick:
                pick = next((n for n in names if os.path.basename(n) == os.path.basename(
                    target_path(c))), None)
            if not pick and names:
                pick = names[0]
            if not pick:
                raise RuntimeError("tar 包内没有可用文件")
            ti = t.getmember(pick)
            if ti.size > MAX_MEMBER_BYTES:
                raise RuntimeError("包内文件过大 %d MB（上限 200MB）" % (ti.size // 1048576))
            f = t.extractfile(pick)
            if not f:
                return b""
            raw = f.read(MAX_MEMBER_BYTES + 1)
            if len(raw) > MAX_MEMBER_BYTES:
                raise RuntimeError("解包结果超过 200MB，已拒绝")
            return raw
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            pick = member if member and member in names else None
            if not pick and member:
                pick = next((n for n in names if n.endswith("/" + member)), None)
            if not pick and names:
                pick = next((n for n in names if os.path.basename(n) == os.path.basename(
                    target_path(c))), names[0])
            if not pick:
                raise RuntimeError("zip 包内没有可用文件")
            zi = z.getinfo(pick)
            if zi.file_size > MAX_MEMBER_BYTES:
                raise RuntimeError("包内文件过大 %d MB（上限 200MB）" % (zi.file_size // 1048576))
            with z.open(pick) as zf:
                raw = zf.read(MAX_MEMBER_BYTES + 1)
            if len(raw) > MAX_MEMBER_BYTES:
                raise RuntimeError("解包结果超过 200MB，已拒绝")
            return raw
    raise RuntimeError("未知 package 类型: %s" % kind)


def validate_payload(c, raw):
    min_size = int(c.get("min_size") or 1000)
    if len(raw) < min_size:
        raise RuntimeError("解包后体积过小 %d < %d" % (len(raw), min_size))
    if c.get("require_elf", True) and not raw.startswith(b"\x7fELF"):
        # 脚本类项目允许关闭 ELF 校验（require_elf=false）
        raise RuntimeError("解包结果不是 ELF 可执行文件")
    return True


def status():
    c = cfg()
    t = target_path(c)
    cur = local_version(c)
    lat = read_kv(os.path.join(CONSOLE, PID + "-latest")).get("version", "")
    size = os.path.getsize(t) if os.path.isfile(t) else 0
    pr = read_text(os.path.join(CONSOLE, "progress." + PID)).strip().split("|")
    out = {
        "version": cur,
        "latest": lat,
        "has_update": "1" if lat and vkey(lat) > vkey(cur) else "0",
        "running": is_running(c),
        "enabled": "0" if os.path.exists(disabled_file()) else "1",
        "binary_size": str(size),
        "target_exists": "1" if os.path.isfile(t) else "0",
        "prev_available": "1" if os.path.isfile(t + ".prev") else "0",
        "adapter": "generic",
        "target": t,
        "progress": "%s|%s|%s" % (pr[0], pr[1], pr[2]) if len(pr) >= 3 else "",
    }
    for k, v in out.items():
        print("%s=%s" % (k, v))


def check(channel=None):
    c = cfg()
    if not channel:
        channel = (c.get("channel") or "beta").strip()
    if channel not in ("beta", "stable"):
        channel = "beta"
    progress("check", "20", "查询上游 releases")
    rel, src = fetch_releases(c, channel)
    pick = pick_release(rel, channel)
    if not pick:
        raise RuntimeError("没有符合条件的 release")
    tag = pick.get("tag_name", "")
    cur = local_version(c)
    kv = {
        "version": tag, "tag": tag, "local": cur,
        "has_update": "1" if vkey(tag) > vkey(cur) else "0",
        "published": pick.get("published_at", ""),
        "prerelease": "1" if pick.get("prerelease") else "0",
        "source": src, "channel": channel,
        "asset": asset_name(c, tag),
    }
    write_kv(os.path.join(CONSOLE, PID + "-latest"), kv)
    progress("check", "100", "本地 %s / 上游 %s" % (cur, tag))
    for k, v in kv.items():
        print("%s=%s" % (k, v))


def backup(c, t, cur):
    os.makedirs(BK, exist_ok=True)
    bid = "%s-%s" % (PID, time.strftime("%Y%m%d-%H%M%S"))
    bd = os.path.join(BK, bid)
    os.makedirs(bd, exist_ok=True)
    if os.path.isfile(t):
        shutil.copy2(t, os.path.join(bd, "payload"))
    with open(os.path.join(bd, "VERSION"), "w", encoding="utf-8") as f:
        f.write(cur + "\n")
    return bid, bd


def restore(c, t, bd, old_ver):
    src = os.path.join(bd, "payload")
    if os.path.isfile(src):
        tmp = t + ".restore"
        shutil.copy2(src, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, t)
    elif os.path.isfile(t + ".prev"):
        shutil.copy2(t + ".prev", t)
        os.chmod(t, 0o755)
    set_version(c, old_ver)


def update(tag=None):
    c = cfg()
    t = target_path(c)
    cur = local_version(c)
    was_running = is_running(c) == "1"
    if not tag:
        kv = read_kv(os.path.join(CONSOLE, PID + "-latest"))
        tag = kv.get("version", "")
        if not tag:
            rel, _ = fetch_releases(c, c.get("channel") or "beta")
            tag = (pick_release(rel, c.get("channel") or "beta") or {}).get("tag_name", "")
    if not tag:
        raise RuntimeError("无法确定目标版本")
    if tag == cur:
        print("ok=1\nalready=1\nversion=%s" % tag)
        return 0

    progress("apply", "5", "开始更新 %s -> %s" % (cur or "未安装", tag))
    bid, bd = backup(c, t, cur)
    data, src = download_package(c, tag)
    sha = hashlib.sha256(data).hexdigest()
    log("downloaded %s bytes=%d sha256=%s src=%s" % (tag, len(data), sha, src))
    progress("apply", "45", "包校验中")
    raw = extract(c, data)
    validate_payload(c, raw)

    os.makedirs(os.path.dirname(t), exist_ok=True)
    tmp = t + ".new"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.chmod(tmp, 0o755)
    if os.path.isfile(t):
        shutil.copy2(t, t + ".prev")
    progress("apply", "60", "替换目标文件")
    if was_running and c.get("stop_cmd"):
        run_shell(c.get("stop_cmd"))
    os.replace(tmp, t)
    set_version(c, tag)
    progress("apply", "80", "已替换，健康检查")

    if was_running and c.get("start_cmd"):
        ok, msg = run_shell(c.get("start_cmd"))
        if not ok or not health_ok(c):
            restore(c, t, bd, cur)
            log("健康检查失败，已回滚: %s" % msg)
            progress("apply", "0", "健康检查失败，已自动回滚")
            print("error=health_check_failed_rolled_back")
            return 1
    elif not was_running and os.path.exists(disabled_file()):
        # 原本停用：绝不拉起，但确保没有残留进程
        if c.get("stop_cmd"):
            run_shell(c.get("stop_cmd"))

    progress("apply", "100", "更新完成 %s" % tag)
    log("updated %s -> %s backup=%s src=%s" % (cur, tag, bid, src))
    print("ok=1\nversion=%s\nbackup=%s\nsha256=%s\nsource=%s\nrunning=%s"
          % (tag, bid, sha, src, is_running(c)))
    return 0


def rollback():
    c = cfg()
    t = target_path(c)
    cur = local_version(c)
    if os.path.isfile(t + ".prev"):
        if c.get("stop_cmd"):
            run_shell(c.get("stop_cmd"))
        shutil.copy2(t + ".prev", t)
        os.chmod(t, 0o755)
    else:
        bks = sorted([d for d in os.listdir(BK) if d.startswith(PID + "-")], reverse=True) \
            if os.path.isdir(BK) else []
        if not bks:
            print("error=no_backup")
            return 1
        bd = os.path.join(BK, bks[0])
        if not os.path.isfile(os.path.join(bd, "payload")):
            print("error=backup_payload_missing")
            return 1
        restore(c, t, bd, "")
    old = ""
    bks = sorted([d for d in os.listdir(BK) if d.startswith(PID + "-")], reverse=True) \
        if os.path.isdir(BK) else []
    for d in bks:
        v = read_text(os.path.join(BK, d, "VERSION")).strip()
        if v and v != cur:
            old = v
            break
    set_version(c, old or cur)
    if c.get("start_cmd") and not os.path.exists(disabled_file()):
        run_shell(c.get("start_cmd"))
    log("rolled back to %s" % (old or cur))
    print("ok=1\nversion=%s" % (old or cur))
    return 0


def toggle(action):
    c = cfg()
    f = disabled_file()
    os.makedirs(os.path.dirname(f), exist_ok=True)
    if action == "disable":
        if c.get("stop_cmd"):
            run_shell(c.get("stop_cmd"))
        with open(f, "w", encoding="utf-8") as h:
            h.write("disabled\n")
        print("ok=1\nrunning=0\nenabled=0")
        return 0
    if action == "enable":
        if os.path.exists(f):
            os.remove(f)
        ok, msg = run_shell(c.get("start_cmd") or "")
        log("enable: %s %s" % (ok, msg))
        print("ok=%s\nrunning=%s\nenabled=1\ndetail=%s"
              % ("1" if ok else "0", is_running(c), msg[:120]))
        return 0 if ok else 1
    raise RuntimeError("未知操作: %s" % action)


def main():
    if len(sys.argv) < 2:
        print("error=missing_command")
        return 2
    cmd = sys.argv[1]
    os.makedirs(WORK, exist_ok=True)
    try:
        if cmd == "status":
            status()
        elif cmd == "check":
            check(sys.argv[2] if len(sys.argv) > 2 else None)
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
