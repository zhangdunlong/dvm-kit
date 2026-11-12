#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare AAAA 记录同步（DDNS）

把手机当前的公网 IPv6 同步到 Cloudflare 的多条 AAAA 记录。

环境变量：
    CF_API_TOKEN   Zone.DNS.Edit 权限的令牌
    CF_ZONE_ID     域名 Zone ID（域名概览页右下角）
    DDNS_RECORDS   逗号分隔，如 "dash.example.com,ql.example.com"
    IPV6_FILE      IPv6 地址文件，默认 /tmp/ipv6-current.txt
    DDNS_TTL       默认 60

安全护栏：
    只接受 2000::/3 公网单播地址。手机上常同时存在 ULA(fdxx::) 与链路本地(fe80::)，
    一旦误写进公网 DNS，整批域名会立刻失效，所以这里宁可跳过也不写错。
"""
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("需要 requests： pip install requests")

API = "https://api.cloudflare.com/client/v4"


def env(key, default=""):
    return (os.environ.get(key) or default).strip()


def get_ipv6(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print("❌ 找不到 %s —— IPv6 守护脚本是否在运行？" % path)
        return ""
    except Exception as e:
        print("❌ 读取 %s 失败：%s" % (path, e))
        return ""


def is_public_v6(addr):
    a = (addr or "").lower()
    if not a or ":" not in a:
        return False
    # 2000::/3 公网单播：首位十六进制是 2 或 3
    return a[0] in ("2", "3")


def cf(method, path, token, **kw):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        r = requests.request(method, API + path, headers=headers, timeout=25, **kw)
        return r.json()
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def main():
    token = env("CF_API_TOKEN")
    zone = env("CF_ZONE_ID")
    records = [x.strip() for x in env("DDNS_RECORDS").split(",") if x.strip()]
    ipv6_file = env("IPV6_FILE", "/tmp/ipv6-current.txt")
    ttl = int(env("DDNS_TTL", "60") or 60)

    if not token or not zone:
        print("❌ 缺少 CF_API_TOKEN / CF_ZONE_ID")
        return 1
    if not records:
        print("❌ 缺少 DDNS_RECORDS")
        return 1

    ipv6 = get_ipv6(ipv6_file)
    if not is_public_v6(ipv6):
        print("⚠️  文件中的地址不是公网 IPv6（%r），跳过本次同步以免写坏 DNS" % ipv6)
        return 1

    print("当前地址：%s" % ipv6)
    ok_n = changed = 0

    for name in records:
        d = cf("GET", "/zones/%s/dns_records?type=AAAA&name=%s" % (zone, name), token)
        if not d.get("success"):
            print("  %-28s 查询失败：%s" % (name, d.get("errors")))
            continue

        rows = d.get("result") or []
        if not rows:
            d2 = cf("POST", "/zones/%s/dns_records" % zone, token,
                    json={"type": "AAAA", "name": name, "content": ipv6,
                          "ttl": ttl, "proxied": False})
            if d2.get("success"):
                print("  %-28s [新建] -> %s" % (name, ipv6))
                ok_n += 1
                changed += 1
            else:
                print("  %-28s 创建失败：%s" % (name, d2.get("errors")))
            continue

        rec = rows[0]
        if rec.get("content") == ipv6:
            print("  %-28s = %s（无变化）" % (name, ipv6))
            ok_n += 1
            continue

        d3 = cf("PATCH", "/zones/%s/dns_records/%s" % (zone, rec["id"]), token,
                json={"content": ipv6, "ttl": ttl})
        if d3.get("success"):
            print("  %-28s [更新] %s -> %s" % (name, rec.get("content"), ipv6))
            ok_n += 1
            changed += 1
        else:
            print("  %-28s 更新失败：%s" % (name, d3.get("errors")))

    print("\n完成：%d/%d 正常，%d 条已更新" % (ok_n, len(records), changed))
    return 0 if ok_n == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
