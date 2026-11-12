#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent2API 上游版本检查（在 DroidVM chroot 内运行，只用标准库）

输出 key=value 供 updatectl.sh / 控制台读取：
  version / tag / published / prerelease / assetUrl / shaUrl /
  has_update / local / channel / notes / source

重要：本机 GitHub API（api.github.com）经代理出口返回 403，
但 github.com 网页 / releases.atom / release 下载均可用。
因此主通道用 Atom feed，镜像与 GitHub API 只作兜底。
代理与镜像源可在 /opt/ag2a-console/upstream.conf 配置（面板可改）。
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

REPO = "aimod-cc/Agent2API"
ASSET = "agent2api-server-linux-arm64.tar.gz"
CONSOLE = os.environ.get("AG2A_CONSOLE_DIR", "/opt/ag2a-console")
WORK = os.path.join(CONSOLE, "work")
ATOM = "https://github.com/%s/releases.atom" % REPO
API = "https://api.github.com/repos/%s/releases?per_page=15" % REPO
DEFAULT_MIRRORS = [
    "https://gh-proxy.com/https://github.com/",
    "https://ghfast.top/https://github.com/",
]
DEFAULT_PROXY = "http://127.0.0.1:7890"


def read_kv(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def conf():
    c = read_kv(os.path.join(CONSOLE, "upstream.conf"))
    proxy = c.get("PROXY") or os.environ.get("AG2A_PROXY") or DEFAULT_PROXY
    raw = c.get("MIRRORS")
    mirrors = [x.strip() for x in raw.split(";") if x.strip()] if raw else DEFAULT_MIRRORS
    return proxy, mirrors


def vkey(v):
    """把 v3.5.0-beta.1 解析成可比较元组；正式版排在预发布之后。"""
    v = (v or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]([A-Za-z]+)[-. ]?(\d*))?", v)
    if not m:
        return (0, 0, 0, 0, 0)
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4)
    rank = 1 if not pre else 0
    order = {"alpha": 0, "beta": 1, "rc": 2, "pre": 1}.get((pre or "").lower(), 1)
    num = int(m.group(5) or 0)
    return (major, minor, patch, rank, order * 1000 + num)


def opener(use_proxy, proxy):
    if use_proxy and proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url, proxy, timeout=25):
    """先走代理，再直连；返回 bytes。"""
    last = ""
    for use_proxy in (True, False):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "ag2a-console/2.1",
                "Accept": "application/atom+xml,application/json,*/*",
            })
            with opener(use_proxy, proxy).open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = "%s: %s" % ("proxy" if use_proxy else "direct", e)
    raise RuntimeError(last or "unreachable")


def parse_atom(data):
    """把 GitHub releases.atom 转成统一 release 列表。"""
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(data)
    out = []
    for e in root.findall("a:entry", ns):
        href = ""
        for link in e.findall("a:link", ns):
            if link.get("rel") in ("alternate", None):
                href = link.get("href", "")
                break
        tag = href.rstrip("/").rsplit("/", 1)[-1] if "/tag/" in href else ""
        if not tag:
            title = (e.findtext("a:title", "", ns) or "").strip()
            if re.match(r"^v?\d+\.\d+\.\d+", title):
                tag = title.strip()
        if not tag:
            continue
        updated = (e.findtext("a:updated", "", ns) or "").strip()
        body = (e.findtext("a:content", "", ns) or "").strip()
        out.append({
            "tag_name": tag,
            "published_at": updated,
            "prerelease": bool(re.search(r"-(alpha|beta|rc|pre|nightly|dev|snapshot)\b", tag, re.I)),
            "draft": False,
            "body": re.sub(r"<[^>]+>", " ", body)[:400],
            "assets": [],
        })
    return out


def get_releases(proxy, mirrors):
    """主：github.com Atom；兜底：镜像 Atom；最后：GitHub API。"""
    errs = []
    urls = [ATOM]
    for m in mirrors:
        urls.append(m + ATOM)
    urls.append(API)
    for u in urls:
        try:
            data = http_get(u, proxy)
            if b"<feed" in data[:400] or b"<entry" in data[:2000]:
                rel = parse_atom(data)
                if rel:
                    return rel, u
            j = json.loads(data.decode("utf-8", "replace"))
            if isinstance(j, list) and j:
                return j, u
        except Exception as e:
            errs.append("%s -> %s" % (u.split("/")[2], str(e)[:80]))
    raise RuntimeError("all upstream failed: " + " | ".join(errs))


def main():
    auto = read_kv(os.path.join(CONSOLE, "auto.conf"))
    channel = (sys.argv[1] if len(sys.argv) > 1 else auto.get("CHANNEL", "beta")).strip()
    if channel not in ("beta", "stable"):
        channel = "beta"
    local = read_kv(os.path.join(WORK, "version")).get("version", "").strip()
    proxy, mirrors = conf()

    try:
        rel, source = get_releases(proxy, mirrors)
    except Exception as e:
        print("error=upstream_unreachable")
        print("detail=%s" % str(e).replace("\n", " ")[:200])
        return 1

    pick = None
    for r in rel:
        if r.get("draft"):
            continue
        if channel == "stable" and r.get("prerelease"):
            continue
        pick = r
        break
    if not pick:
        print("error=no_release")
        return 1

    tag = pick.get("tag_name", "")
    asset_url = ""
    sha_url = ""
    for a in pick.get("assets", []):
        n = a.get("name", "")
        if n == ASSET:
            asset_url = a.get("browser_download_url", "")
        elif n == ASSET + ".sha256":
            sha_url = a.get("browser_download_url", "")
    if not asset_url:
        asset_url = "https://github.com/%s/releases/download/%s/%s" % (REPO, tag, ASSET)
    if not sha_url:
        sha_url = asset_url + ".sha256"

    out = {
        "version": tag,
        "tag": tag,
        "published": pick.get("published_at", ""),
        "prerelease": "1" if pick.get("prerelease") else "0",
        "assetUrl": asset_url,
        "shaUrl": sha_url,
        "assetName": ASSET,
        "has_update": "1" if vkey(tag) > vkey(local) else "0",
        "local": local,
        "channel": channel,
        "notes": (pick.get("body", "") or "").split("\n")[0][:160],
        "source": source,
    }
    for k in ("version", "tag", "published", "prerelease", "assetUrl", "shaUrl",
              "assetName", "has_update", "local", "channel", "notes", "source"):
        print("%s=%s" % (k, out[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
