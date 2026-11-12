# DVM Kit · 把安卓手机变成服务器

一套在 Android 手机上运行自托管服务的完整方案，包含**部署脚本、管理面板、状态仪表盘和配套 App**。

不需要 Root 也能起步，有 Root 可以跑得更彻底。适配两种接入方式：**公网 IPv6 直连** 和 **Cloudflare Tunnel**（无公网 IP 也能用）。

```
                         ┌──────────────────────────────┐
   公网 IPv6  ───────────▶│  Caddy :443  (TLS 终止/反代)  │
   Cloudflare Tunnel ───▶│                              │
                         └───────────┬──────────────────┘
                                     │
        ┌────────────────┬───────────┼───────────┬────────────────┐
        ▼                ▼           ▼           ▼                ▼
   青龙面板 :5700   更新中心 :3077  仪表盘 :8765  Agent2API :3065  其他
        │                │           │           │
        └────────────────┴───────────┴───────────┘
                                     │
                              ┌──────▼──────┐
                              │  DVM 控制台  │  ← Android App
                              │ 内外网自动切换│
                              └─────────────┘
```

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| **一条命令起服务** | Caddy 自动签证书、反代、HTTP→HTTPS 跳转 |
| **双线路接入** | 有公网 IPv6 就直连（零成本、无限速）；没有就用 Cloudflare Tunnel |
| **图形化管理** | 项目更新中心可检查/更新/回滚各个服务，带备份与健康检查 |
| **状态可视** | 仪表盘显示电量、温度、CPU、内存、磁盘、服务健康、证书到期、DDNS 状态 |
| **手机 App** | 记住账号密码，自动判断当前该走内网还是外网 |
| **自动更新** | 定时检查上游版本，失败自动回滚 |
| **智能体引导** | 附 `AGENT.md`，把仓库丢给 AI 助手就能一步步带你装 |

---

## 快速开始

> 完整步骤见 [docs/01-quickstart.md](docs/01-quickstart.md)

```bash
# 1. 克隆
git clone https://github.com/<you>/dvm-kit.git && cd dvm-kit

# 2. 跑安装脚本（会检测环境并安装依赖）
bash scripts/install.sh

# 3. 按提示填写你的域名 / 端口，然后启动
bash scripts/start.sh
```

装完后：

- 面板：`http://<手机IP>:3077/ag2a/`
- 仪表盘：`http://<手机IP>:8765/`
- App：安装 `release/dvm-kit-*.apk`，填内网/外网地址即可

---

## 两种接入方式，怎么选

| | **A. IPv6 直连** | **B. Cloudflare Tunnel** |
|---|---|---|
| 前提 | 运营商给公网 IPv6（移动/联通/电信多数已给） | 有 Cloudflare 账号 + 一个域名 |
| 成本 | 0 | 0（免费套餐够用） |
| 速度 | 取决于宽带上行，通常最快 | 经 CF 边缘，国内可能绕路 |
| 稳定性 | 依赖运营商 IPv6 质量 | 依赖出口网络到 CF 的连通性 |
| 适合 | 家宽、IPv6 稳定 | 只有 IPv4 / IPv6 不稳 / 需要固定域名 |
| 文档 | [docs/02-deploy-ipv6.md](docs/02-deploy-ipv6.md) | [docs/03-deploy-tunnel.md](docs/03-deploy-tunnel.md) |

两者**可以同时开**：IPv6 做主力，Tunnel 做纯 IPv4 客户端的兜底。

---

## 组件清单

| 组件 | 作用 | 是否必需 |
|---|---|---|
| `scripts/` | 启动、守护、DDNS、隧道脚本 | 必需 |
| `console/` | 项目更新中心（Web 管理面板） | 推荐 |
| `dashboard/` | 状态仪表盘 | 推荐 |
| `android/` | DVM 控制台 App 源码 | 可选 |
| Caddy | HTTPS 终止与反代 | 必需（用隧道时可省） |
| 青龙面板 | 定时任务，需单独部署 | 可选 |

各服务的详细部署见 [docs/04-services.md](docs/04-services.md)。

---

## Android App

`android/` 是一个 Kotlin WebView 壳，做的事情很克制：

1. **双地址**：同时保存内网（`http://192.168.x.x:5700`）和外网（`https://ql.example.com`）
2. **自动探测**：打开时并发 ping 两个地址，2.5 秒超时，哪个通用哪个
3. **凭据记忆**：账号密码用 Android Keystore 的 AES-256-GCM 加密后存本机，页面加载后自动填充登录表单
4. **多服务入口**：青龙 / 更新中心 / 仪表盘，点哪个进哪个

构建：

```bash
cd android
./gradlew assembleDebug        # 需要 JDK 17+ 与 Android SDK 34
```

详细说明见 [docs/05-app.md](docs/05-app.md)。

---

## 用 AI 助手带你装

把仓库地址丢给任意支持读文件的 AI 助手（Claude / GPT / 通义 等），说：

> 读一下 `AGENT.md`，按里面的步骤带我部署

`AGENT.md` 里写了完整的分步检查清单，助手会先探测你的环境（是否有 IPv6、是否 Root、端口占用），再决定走哪条路线。见 [AGENT.md](AGENT.md)。

---

## 安全说明

这套方案会把服务暴露到公网，请务必读完 [SECURITY.md](SECURITY.md)。几个硬性建议：

- 面板和任务系统**必须**启用强密码，**强烈建议**开启 TOTP 两步验证
- 不要把 Clash / Mihomo 的控制端口暴露到公网（默认会监听 `0.0.0.0:9090`，等于把代理配置交出去）
- ADB over TCP（5555）用完就关
- 隧道方案下，只有你显式配置的 hostname 会被公开

---

## 目录结构

```
dvm-kit/
├── AGENT.md              智能体分步引导
├── SECURITY.md           安全清单
├── scripts/              部署与守护脚本
│   ├── install.sh        环境检测与依赖安装
│   ├── start.sh          启动全部服务
│   ├── run-tunnel.sh     Cloudflare Tunnel 启动器
│   ├── ipv6-guard.sh     IPv6 地址守护与 DDNS 触发
│   ├── ddns-cloudflare.py  AAAA 记录同步
│   └── Caddyfile.example Caddy 配置模板
├── console/              项目更新中心
├── dashboard/            状态仪表盘
├── android/              DVM 控制台 App
└── docs/                 部署文档
```

---

## 许可

MIT。脚本和代码可自由使用、修改、商用，风险自负。
