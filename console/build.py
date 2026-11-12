#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 console/ 打成可 adb push 的 zip（保留 Unix 权限位）"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "ag2a-console.zip")
SKIP_DIRS = {"_test", "__pycache__", ".git", "work", "tasks", "backups"}
SKIP_SUFFIX = (".pyc", ".pyo", ".bak", ".tmp")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for base, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIX) or name == "build.py":
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, HERE).replace(os.sep, "/")
            arc = "console/" + rel
            mode = 0o755 if (rel.endswith((".sh", ".cgi", ".py"))) else 0o644
            info = zipfile.ZipInfo(arc)
            info.external_attr = (mode & 0xFFFF) << 16
            info.create_system = 3
            with open(full, "rb") as f:
                z.writestr(info, f.read())
            print(("%o" % mode), arc)
print("OK ->", OUT, os.path.getsize(OUT), "bytes")
