# 方案 B · Cloudflare Tunnel

> 适用于：没有公网 IPv4、IPv6 不稳定或被封、需要固定域名且不想动光猫防火墙的场景。

---

## 前置条件

- Cloudflare 账号（免费套餐足够）
- 一个域名，NS 已经托管到 Cloudflare
- 手机能访问 `api.cloudflare.com` 与 CF 边缘

---

## 第一步：创建隧道

```bash
cloudflared tunnel login          # 浏览器授权，会把证书写到 ~/.cloudflared/cert.pem
cloudflared tunnel create dvm     # 记下输出的 Tunnel ID
```

创建后会生成 `~/.cloudflared/<TUNNEL_ID>.json`（隧道凭据），**这个文件等同于密码，权限设为 600**。

---

## 第二步：写 ingress 配置

`~/.cloudflared/config.yml`：

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: dash.example.com
    service: http://127.0.0.1:8765
  - hostname: ql.example.com
    service: http://127.0.0.1:5700
  - hostname: api.example.com
    service: http://127.0.0.1:3065
  - service: http_status:404        # 兜底，必须放最后
```

> 兜底规则不可省略，否则未匹配的请求会被拒绝，日志里只看到含糊的错误。

---

## 第三步：绑定 DNS

```bash
cloudflared tunnel route dns dvm dash.example.com
cloudflared tunnel route dns dvm ql.example.com
cloudflared tunnel route dns dvm api.example.com
```

这会自动创建 `CNAME -> <TUNNEL_ID>.cfargotunnel.com`，且**必须是橙云（Proxied）**。

---

## 第四步：常驻运行

```bash
# 前台测试（确认能连上）
cloudflared --config /root/.cloudflared/config.yml tunnel run

# 常驻
bash scripts/run-tunnel.sh
```

`run-tunnel.sh` 会：

1. 用 `setsid nohup` 脱离终端
2. 把日志写到 `.logs/cloudflared.log`
3. 记录 PID 到 `.dvmkit/pids/cloudflared.pid`

---

## 第五步：验证

```bash
# 1. 进程
ps aux | grep "[c]loudflared"

# 2. 云端连接状态（需要开启 metrics）
curl -s http://127.0.0.1:20241/metrics | grep '^cloudflared_tunnel_ha_connections'
# 期望 >= 1

# 3. 外部访问
curl -s -o /dev/null -w "%{http_code}\n" https://dash.example.com/
```

---

## 传输协议选择（重要）

cloudflared 支持 QUIC(UDP 7844) 和 HTTP/2(TCP 443) 两种传输。

| 协议 | 优点 | 缺点 |
|---|---|---|
| QUIC | 理论上更快、抗丢包 | **出站 UDP 被丢时反而最不稳**，日志刷 `timeout: no recent network activity` |
| HTTP/2 | 走 TCP 443，穿透性好 | 被定向阻断时报 `HTTP/2 connection is blocked or unreachable` |

**建议**：先用 QUIC；如果日志里反复出现 `no recent network activity` 且频繁重连，
换成 HTTP/2：

```bash
cloudflared --protocol http2 --config ... tunnel run
```

> cloudflared 不走 `HTTPS_PROXY` 环境变量，配代理对它无效。它的连通性完全取决于当前网络的出站策略。

---

## 边缘节点选择

默认由 CF 自动调度。如果发现连接落在很远的机房（日志里的 `location=`），
可以固定 edge IP：

```bash
cloudflared --edge-ip-version 4 \
  --edge 198.41.192.57:7844 --edge 198.41.200.233:7844 \
  --config ... tunnel run
```

在手机上尤其有用：**手机上的代理软件会劫持 DNS 返回 fake-ip（198.18.x.x）**，
导致 cloudflared 解析到私有地址直接握手失败。固定 edge IP 可以绕开这个坑。

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 外部 530 | 隧道瞬时无健康连接 | 看日志是否在反复重连；换协议或换网络 |
| 外部 1033 | 隧道未运行 / 凭据错误 | 检查进程与 `credentials-file` |
| 外部 522 | CF 连不上源站 | 本地 `curl 127.0.0.1:8765` 是否通 |
| 外部 404 | ingress 没匹配上 | 检查 hostname 拼写与兜底规则位置 |
| 日志 `timeout: no recent network activity` | 出站 UDP 被丢 | 换 `--protocol http2` |
| 日志 `HTTP/2 connection is blocked` | TCP 443 被定向阻断 | 换 QUIC，或换网络 |
| 客户端很慢（3~10s） | 落了远端机房 | 见上方边缘节点选择 |

---

## 性能预期

隧道方案的实际体验：

- 从中国大陆访问，边缘通常落在香港/新加坡/美西
- 首次 TLS 握手 0.5~6 秒不等，取决于落点
- **约有 10~25% 的请求会超时**（尤其在移动网络下）
- 因此：**主入口建议用 IPv6 直连，隧道只做兜底**

这也是本项目不建议把隧道作为唯一入口的原因。

---

## 优点与局限

**优点**
- 完全不需要公网 IP，不碰光猫
- 自动 HTTPS，不用管证书
- 可以只暴露指定 hostname
- 内网穿透稳定（相比各类免费 frp）

**局限**
- 经 CF 边缘，国内访问可能绕路
- 免费套餐对长连接（WebSocket）有超时限制
- 依赖 `api.cloudflare.com` 与 CF 边缘的可达性
- 手机上的代理软件可能干扰 cloudflared 的 DNS 解析
