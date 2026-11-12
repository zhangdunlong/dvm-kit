# AGENT.md · 交给 AI 助手的分步部署剧本

> 你是被用户委托的 AI 助手。本文件是你的操作手册。
> **先探测，再决策，再动手**。每一步都要验证通过才进入下一步。
> 遇到不确定的选项，停下来问用户，不要替他猜。

---

## 0. 你和用户的目标

把一台 Android 手机变成可公网访问的自托管服务器，跑起：

- 状态仪表盘（8765）
- 项目更新中心（3077）
- 可选：青龙面板（5700）、AI 网关（3065）

并且产出 App 让用户随时管理。

**接入方式二选一**：

| | IPv6 直连 | Cloudflare Tunnel |
|---|---|---|
| 需要 | 运营商分配公网 IPv6 | CF 账号 + 域名 |
| 速度 | 快（取决上行带宽） | 一般（经 CF 边缘） |
| 适合 | 家宽、IPv6 稳定 | 只有 IPv4、IPv6 被封 |

---

## 1. 第一阶段：环境探测（不要跳过）

逐条执行，把结果记录下来告诉用户。

```bash
# 1.1 架构
uname -m

# 1.2 运行环境判别
[ -n "$TERMUX_VERSION" ] && echo "termux" || echo "not-termux"
[ -f /system/build.prop ] && echo "android" || echo "linux"
[ -f /etc/os-release ] && cat /etc/os-release | head -3

# 1.3 是否有 root
id -u

# 1.4 公网 IPv6（关键！）
ip -6 addr show | grep "scope global" | grep -vE "fe80|fd"

# 1.5 端口占用
ss -ltnp 2>/dev/null | head -30 || netstat -tlnp 2>/dev/null | head -30

# 1.6 已装组件
command -v python3 caddy cloudflared curl
```

**判定规则**：

- 1.4 有 `2408:` / `2402:` / 类似开头 → **可以走 IPv6 方案**（推荐）
- 1.4 为空 → 只能走 Tunnel 方案
- 1.5 发现 443 被占用 → 先解决占用再继续
- 1.6 缺 python3 → 必须先装（Termux: `pkg install python`；Debian: `apt install python3`）

把结论用一句话告诉用户，例如：
> 检测到公网 IPv6 `2408:xxxx`，建议走 IPv6 直连方案，速度更快且零成本。

---

## 2. 第二阶段：装依赖

### Termux 路线

```bash
pkg update
pkg install -y python curl openssl-tool
# Caddy 走 IPv6 方案时必需；官方仓库没有 aarch64 包，用这里的方式装：
# 参考 https://caddyserver.com/download 选择 linux/arm64，下载后解压到 $PREFIX/bin
```

### Debian / chroot 路线

```bash
apt update
apt install -y python3 curl ca-certificates
# Caddy 用官方 apt 源
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

**注意**：chroot 环境需要 `/proc`、`/sys`、`/dev` 已挂载，否则服务起不来。

---

## 3. 第三阶段：初始化本项目

```bash
cd dvm-kit
bash scripts/install.sh
```

这个脚本会：
1. 探测架构与环境
2. 检查公网 IPv6
3. 检查端口占用
4. 生成 `.dvmkit/dvmkit.env` 与 `scripts/Caddyfile`

**你需要接着做**：让用户提供以下信息，然后填入 `.dvmkit/dvmkit.env`：

| 字段 | 说明 | 示例 |
|---|---|---|
| `DOMAIN_PANEL` | 面板域名 | `dash.example.com` |
| `DOMAIN_QL` | 青龙域名 | `ql.example.com` |
| `ACME_EMAIL` | 证书通知邮箱 | `you@example.com` |
| `CF_API_TOKEN` | 仅 DNS-01 需要 | `cfat_xxx` |

同时把 `scripts/Caddyfile` 里的 `example.com` 换成真实域名。

---

## 4. 第四阶段：按方案配置

### 方案 A · IPv6 直连

完整步骤见 `docs/deploy-ipv6.md`。要点：

1. 确认路由器/光猫**放行 IPv6 入站 443**（多数光猫默认拦截，需在防火墙里开）
2. Cloudflare（或你的 DNS 商）添加 **AAAA 记录**指向手机 IPv6
3. 配置 DDNS 自动更新（`scripts/ddns-cloudflare.py`）
4. 证书用 **DNS-01**（80 端口常被运营商封），需要 `CF_API_TOKEN`

### 方案 B · Cloudflare Tunnel

完整步骤见 `docs/deploy-tunnel.md`。要点：

1. `cloudflared tunnel login` → `cloudflared tunnel create dvm`
2. 写 `~/.cloudflared/config.yml` 的 ingress
3. `cloudflared tunnel route dns dvm dash.example.com`
4. 用 `scripts/run-tunnel.sh` 常驻

---

## 5. 第五阶段：启动与验证

```bash
bash scripts/start.sh
```

验证清单（逐条确认后再告诉用户成功）：

```bash
# 本地端口
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/     # 期望 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3077/ag2a/ # 期望 200

# 面板令牌
cat console/token

# 外部访问（把域名换成真实的）
curl -s -o /dev/null -w "%{http_code}\n" https://dash.example.com/
```

如果外部不通：
1. 先确认本地通
2. 再确认 DNS 解析到正确 IPv6：`nslookup dash.example.com`
3. 再确认 Caddy 拿到了证书：`journalctl -u caddy` 或看 `.logs/caddy.log`
4. 最后检查防火墙

---

## 6. 第六阶段：App（可选）

APK 在 `release/` 目录，或让用户自己构建：

```bash
cd android && ./gradlew assembleDebug
```

装好后在 App 里填：
- 内网地址：`http://<手机局域网IP>:5700`
- 外网地址：`https://ql.example.com`
- 账号密码

App 会自动探测哪条线通。

**注意**：部分国产 ROM（ColorOS / HyperOS）默认禁止 ADB 静默安装，需要用户手动点安装包确认。

---

## 7. 常见故障对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 外部 530 / 1033 | 隧道没连上 | 看 `cloudflared` 日志，检查出站是否被拦 |
| 外部 522 / 超时 | CF 连不到源站 | 检查 Caddy 是否在跑、源站端口对不对 |
| 证书签发失败 | 80 端口不通 | 改用 DNS-01 |
| 证书 `EOF` | ACME 出站被拦 | 换网络环境或用 DNS-01 + 代理 |
| 只有内网能访问 | 光猫防火墙拦了 IPv6 入站 | 在光猫里放行 443 |
| 端口被占 | 已有服务在跑 | `ss -ltnp` 找出进程 |
| App 装不上 | ROM 禁止 ADB 安装 | 手动点 APK 安装 |

---

## 8. 安全底线（必须提醒用户）

1. **面板密码必须强**，并开启 TOTP 两步验证
2. **不要**把 Clash/Mihomo 的 9090 控制端口暴露到公网
3. ADB over TCP 用完就关
4. 隧道只暴露你显式配置的 hostname

详见 `SECURITY.md`。

---

## 9. 汇报格式建议

每完成一个阶段，用这样的格式告诉用户：

```
✅ 阶段 N 完成
- 做了什么：……
- 验证结果：……（贴关键输出）
- 下一步：……

⚠️ 需要你确认：……
```

不要一次性丢给用户一大段命令让他自己跑。你是执行者，不是说明书。
