#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Caddyfile 补丁（在 DroidVM chroot 内运行，路径为 chroot 内视角）

用法：
  python3 caddy_patch.py add    <file> <输出文件>
  python3 caddy_patch.py remove <file> <输出文件>
  python3 caddy_patch.py check  <file>

在 dash.example.com 站点块内、第一个 8765 反代之前插入 /ag2a 路由。
幂等：已存在标记则返回 unchanged。全程二进制读写，保留原文件换行风格。
"""
import os
import sys

SITE = b"https://dash.example.com {"
PORT = b"3077"
MARK = "===== Agent2API 远程更新控制台（由 enable-remote.sh 生成）=====".encode("utf-8")
BLOCK_LINES = [
    b"\t# " + MARK,
    b"\thandle_path /ag2a/* {",
    b"\t\treverse_proxy 127.0.0.1:" + PORT + b" {",
    b"\t\t\theader_up X-Real-IP {remote_host}",
    b"\t\t}",
    b"\t}",
]


def main():
    if len(sys.argv) < 3:
        print("usage: caddy_patch.py add|remove|check <file> [out]", file=sys.stderr)
        return 2
    action, path = sys.argv[1], sys.argv[2]
    with open(path, "rb") as f:
        raw = f.read()
    lines = raw.splitlines(keepends=True)
    nl = b"\r\n" if b"\r\n" in raw else b"\n"

    if action == "check":
        print("present" if any(MARK in l for l in lines) else "absent")
        return 0

    out = sys.argv[3] if len(sys.argv) > 3 else path
    if action == "add":
        if any(MARK in l for l in lines):
            print("unchanged")
            return 0
        start = next((i for i, l in enumerate(lines) if l.startswith(SITE)), None)
        if start is None:
            print("site block not found", file=sys.stderr)
            return 3
        at = next((j for j in range(start + 1, len(lines))
                   if b"reverse_proxy" in lines[j] and b"127.0.0.1:8765" in lines[j]), None)
        if at is None:
            print("8765 reverse_proxy not found in site block", file=sys.stderr)
            return 4
        result = lines[:at] + [l + nl for l in BLOCK_LINES] + lines[at:]
    elif action == "remove":
        if not any(MARK in l for l in lines):
            print("unchanged")
            return 0
        result, skip = [], 0
        for l in lines:
            if MARK in l:
                skip = len(BLOCK_LINES) - 1  # MARK 行本身已跳过，再跳过其余 5 行
                continue
            if skip > 0:
                skip -= 1
                continue
            result.append(l)
    else:
        print("unknown action", file=sys.stderr)
        return 2

    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"".join(result))
    os.replace(tmp, out)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
