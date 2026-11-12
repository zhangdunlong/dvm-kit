#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读适配器 · 青龙面板 / Node.js（项目更新中心）

这两个目标不适合无人值守更新：
  · 青龙：更新要走官方 ql update（重装依赖、重建前端），会重建 node_modules，
          现有 30+ 任务的运行环境存在被破坏的风险；
  · Node：替换 120MB 运行时会连带影响青龙、mi-paiai、控制台等多个常驻服务。

因此本适配器只提供 status / check，让面板能显示「当前版本 / 上游版本 / 是否落后」。
update 明确返回 manual_required，绝不擅自替换。

命令协议与 generic_ctl.py 一致（输出 key=value）：
    PUH_PROJECT=qinglong python3 /opt/ag2a-console/misc_ctl.py status
    PUH_PROJECT=qinglong python3 /opt/ag2a-console/misc_ctl.py check
"""
import json
import os
import re
import subprocess
import sys

CONSOLE = os.environ.get("PUH_CONSOLE_DIR") or os.environ.get("AG2A_CONSOLE_DIR") or "/opt/ag2a-console"
PID = os.environ.get("PUH_PROJECT", "").strip()
REG = os.path.join(CONSOLE, "projects.json")
WORK = os.path.join(CONSOLE, "work")

sys.path.insert(0, CONSOLE)
try:
    import upstream as UP
except Exception:
    UP = None


def cfg():
    with open(REG, "r", encoding="utf-8") as f:
        reg = json.load(f)
    for p in reg.get("projects", []):
        if p.get("id") == PID:
            return p
    raise SystemExit("error=project_not_found")


def read_text(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def read_kv(p):
    out = {}
    for line in read_text(p).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_kv(p, kv):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write("%s=%s\n" % (k, str(v).replace("\n", " ")))
    os.replace(tmp, p)


def vkey(v):
    s = (v or "").strip().lstrip("vV")
    pre = bool(re.search(r"-(alpha|beta|rc|pre|dev|nightly|snapshot)", s, re.I))
    nums = [int(x) for x in re.findall(r"\d+", s)][:6]
    nums += [0] * (6 - len(nums))
    return tuple(nums) + (0 if pre else 1,)


def major(v):
    m = re.search(r"(\d+)\.", (v or "").lstrip("vV"))
    return m.group(1) if m else ""


def local_version(c):
    kind = (c.get("local_kind") or "cmd").strip()
    if kind == "yaml":
        txt = read_text(c.get("local_file") or "")
        m = re.search(r"^\s*version:\s*[\"']?([0-9][0-9A-Za-z.\-]*)[\"']?\s*$", txt, re.M)
        return m.group(1) if m else ""
    cmd = (c.get("version_cmd") or "").strip()
    if not cmd:
        return ""
    try:
        out = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20)
        lines = (out.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        return lines[0].strip() if lines else ""
    except Exception:
        return ""


def file_size(c):
    p = (c.get("local_file") or "").strip()
    if p and os.path.isfile(p):
        return os.path.getsize(p)
    tgt = (c.get("version_cmd") or "")
    m = re.search(r"(/[^\s|]+)", tgt)
    if m and os.path.isfile(m.group(1)):
        return os.path.getsize(m.group(1))
    return 0


def fetch(c):
    src_kind = (c.get("src_kind") or "github_atom").strip()
    if src_kind == "nodejs_dist":
        # Node 的 LTS 分支发布频率低，releases.atom 前几条常被新主版本占满，
        # 因此改用官方全量清单，只保留与当前主版本相同的条目。
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(urllib.request.Request(
                "https://nodejs.org/dist/index.json",
                headers={"User-Agent": "puh/3.0"}), timeout=25) as r:
            lst = json.loads(r.read().decode("utf-8", "replace"))
        cur_major = major(local_version(c))
        out = []
        for it in lst:
            v = (it.get("version") or "").strip()
            if not v:
                continue
            if cur_major and major(v) != cur_major:
                continue
            out.append({"tag_name": v, "published_at": it.get("date", ""),
                        "prerelease": False, "draft": False})
        out.sort(key=lambda x: vkey(x["tag_name"]), reverse=True)
        return out, "nodejs.org/dist"
    if not UP:
        raise RuntimeError("upstream 模块不可用")
    repo = (c.get("repo") or "").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repo):
        raise RuntimeError("repo 格式错误: %s" % repo)
    atom = "https://github.com/%s/releases.atom" % repo
    px, mirrors = UP.conf()
    errs = []
    for u in [atom] + [m + atom for m in mirrors]:
        try:
            data = UP.http_get(u, px, timeout=25)
            if b"<feed" in data[:400] or b"<entry" in data[:2000]:
                rel = UP.parse_atom(data)
                if rel:
                    return rel, u
        except Exception as e:
            errs.append(str(e)[:60])
    raise RuntimeError("上游不可达: " + "; ".join(errs))


def pick(c, rel):
    channel = (c.get("channel") or "stable").strip()
    same = bool(c.get("same_major"))
    cur_major = major(local_version(c))
    cand = []
    for r in rel:
        if r.get("draft"):
            continue
        tag = r.get("tag_name", "")
        # 只接受语义化版本：青龙仓库里混有 upgrade-build-* 之类 CI 构建标签
        if not re.match(r"^v?\d+\.\d+", tag):
            continue
        if channel == "stable" and r.get("prerelease"):
            continue
        if same and cur_major and major(tag) != cur_major:
            continue
        cand.append(r)
    if not cand:
        return None
    cand.sort(key=lambda x: vkey(x.get("tag_name", "")), reverse=True)
    return cand[0]


def status():
    c = cfg()
    cur = local_version(c)
    lat = read_kv(os.path.join(CONSOLE, (c.get("latest_file") or (PID + "-latest")))).get("version", "")
    print("version=%s" % cur)
    print("latest=%s" % lat)
    print("has_update=%s" % ("1" if lat and vkey(lat) > vkey(cur) else "0"))
    print("binary_size=%s" % str(file_size(c)))
    print("adapter=custom")
    print("note=%s" % (c.get("notes") or "").replace("\n", " "))


def check(channel=None):
    c = cfg()
    rel, src = fetch(c)
    p = pick(c, rel)
    if not p:
        raise RuntimeError("没有符合条件的 release（可能只发布了其他主版本）")
    tag = p.get("tag_name", "")
    cur = local_version(c)
    kv = {
        "version": tag,
        "tag": tag,
        "local": cur,
        "has_update": "1" if vkey(tag) > vkey(cur) else "0",
        "published": p.get("published_at", ""),
        "prerelease": "1" if p.get("prerelease") else "0",
        "source": src,
        "channel": (c.get("channel") or "stable"),
        "mode": "readonly",
    }
    write_kv(os.path.join(CONSOLE, (c.get("latest_file") or (PID + "-latest"))), kv)
    for k, v in kv.items():
        print("%s=%s" % (k, v))
    return 0


def main():
    if not PID:
        raise SystemExit("error=PUH_PROJECT_missing")
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    try:
        if cmd == "status":
            status()
            return 0
        if cmd == "check":
            return check(sys.argv[2] if len(sys.argv) > 2 else None)
        if cmd in ("update", "apply"):
            print("error=manual_required")
            print("hint=该项目需人工执行官方更新流程（见项目说明）")
            return 2
        if cmd in ("rollback", "enable", "disable", "upload"):
            print("error=unsupported")
            return 2
        print("error=unknown_command")
        return 2
    except Exception as e:
        print("error=%s" % str(e).replace("\n", " ")[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
