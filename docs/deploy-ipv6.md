# 方案 A · 公网 IPv6 直连

> 适用于：运营商已分配公网 IPv6 的环境。速度快、零成本，是首选方案。

---

## 前置条件

- 运营商分配了公网 IPv6（移动/联通/电信多数已默认开启）
- 一个域名，DNS 托管在 Cloudflare（或支持 API 的服务商）
- 手机能长期供电、联网

验证是否有公网 IPv6：

```bash
ip -6 addr show | grep "scope global" | grep -vE "fe80|fd"
```

出现类似 `2408:823c:xxxx` 的地址即为公网可达。

> ⚠️ **注意区分三种地址**：
> - `fe80::` 链路本地 —— 不能用于公网
> - `fdxx::` ULA 私有地址 —— 不能用于公网，**DDNS 千万不要记录这个**
> - `2408:` / `2402:` 等 —— 公网单播，这才是要用的
>
> 手机通常同时存在 **stable-privacy** 和 **temporary** 两种公网地址。
> DDNS 要登记 **stable-privacy**（稳定，服务监听在它上面）；
> 出站连接会走 temporary（隐私地址），两者不同是正常的。

---

## 第一步：放行光猫 / 路由器的 IPv6 入站

这是**最常见的卡点**。多数光猫默认拦截所有 IPv6 入站连接。

1. 登录光猫管理页（通常是 `192.168.1.1`）
2. 找到「安全 / 防火墙 / IPv6 防火墙」设置
3. 放行 **TCP 443** 入站（如果 Caddy 还监听 80，一并放行）
4. 保存并重启光猫

如果光猫后面还有路由器，且是**路由模式**，需要两级都放行；
**桥接模式**则只需在拨号设备上放行。

验证入站是否通（从外网、用手机流量访问）：

```bash
# 在另一台不在同一局域网的设备上执行
curl -6 -s -o /dev/null -w "%{http_code}\n" --max-time 8 "https://[你的IPv6地址]/"
```

返回 `000` 说明入站被拦；返回 `400` / `404` / `302` 说明已经通了。

---

## 第二步：DNS AAAA 记录

在 Cloudflare 添加：

| 类型 | 名称 | 内容 | 代理 |
|---|---|---|---|
| AAAA | `dash` | `2408:xxxx:...` | **关闭（灰云）** |
| AAAA | `ql` | 同上 | 关闭 |

**必须是灰云（DNS only）**。橙云会让流量绕 Cloudflare 边缘，失去直连意义，而且 CF 回源 IPv6 需要额外配置证书。

> 同一主机名不能同时存在 CNAME 与 AAAA（DNS 规范限制）。如果之前配过隧道用的 CNAME，要先删掉。

---

## 第三步：DDNS 自动更新

IPv6 地址会随重播变化，必须自动同步。

`scripts/ddns-cloudflare.py` 做的事情：

1. 读取当前 stable-privacy 公网地址
2. 与 Cloudflare 上的 AAAA 记录比对
3. 不一致就 PATCH 更新

配置环境变量：

```bash
export CF_API_TOKEN="你的令牌"      # 权限：Zone.DNS.Edit
export CF_ZONE_ID="你的ZoneID"
export DDNS_RECORDS="dash.example.com,ql.example.com"
```

建议每 10 分钟跑一次（cron 或 Termux 的 `termux-job-scheduler`）：

```cron
*/10 * * * * /usr/bin/python3 /path/to/scripts/ddns-cloudflare.py
```

**安全护栏**（脚本已内置）：如果读到的地址不是 `2000::/3` 公网段，会跳过本次同步并报错，
避免把 ULA / 链路本地地址写进公网 DNS 导致整批域名失效。

---

## 第四步：Caddy 与证书

编辑 `scripts/Caddyfile`，把 `example.com` 换成真实域名。

**证书方式选 DNS-01**（推荐）：

```caddyfile
https://dash.example.com {
	tls {
		dns cloudflare {env.CF_API_TOKEN}
		resolvers 8.8.8.8 1.1.1.1
		propagation_delay 15s
		propagation_timeout 180s
	}
	encode gzip
	handle_path /ag2a/* {
		reverse_proxy 127.0.0.1:3077
	}
	reverse_proxy 127.0.0.1:8765
}
```

为什么用 DNS-01 而不是 HTTP-01：

- 80 端口常被运营商封锁（家宽普遍封 80）
- HTTP-01 需要 Let's Encrypt 能从公网访问 80
- DNS-01 只需要能访问 Cloudflare API 和 ACME 服务器

启动 Caddy 时必须把 `CF_API_TOKEN` 传进环境，否则会报：

```
provision dns.providers.cloudflare: API token '' appears invalid
```

然后整个配置加载失败，Caddy 起不来。

---

## 第五步：启动与验证

```bash
bash scripts/start.sh
```

验证顺序（**必须按这个顺序排查**）：

```bash
# 1. 本地端口
ss -ltn | grep -E ":(443|8765|3077) "

# 2. 本地 HTTPS
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1/ -H "Host: dash.example.com"

# 3. DNS 解析
nslookup dash.example.com        # 应返回你的 IPv6

# 4. 外部访问（换一台不在内网的设备）
curl -s -o /dev/null -w "%{http_code}\n" https://dash.example.com/
```

任一步失败，对应到下面的排查表。

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 外部 `000` | 光猫防火墙拦了入站 | 放行 IPv6 TCP 443 |
| 外部 `000` 且本地正常 | DNS 没更新 | 手动跑一次 ddns 脚本，检查 AAAA |
| 证书申请 `EOF` | 到 ACME 的出站被拦 | 换网络 / 给 Caddy 配代理 / 改用别的 CA |
| 证书申请超时 | Cloudflare API 不通 | 检查 `CF_API_TOKEN` 与出站 |
| `tlsv1 alert internal error` | Caddy 没有匹配该 SNI 的站点 | 检查 Caddyfile 里的域名拼写 |
| 页面 502 | 反代目标端口没起 | 先确认 `127.0.0.1:8765` 是否 200 |
| 一段时间后突然不通 | IPv6 变了但 DDNS 没跑 | 检查 cron，`/tmp/ipv6-current.txt` 的 mtime |

---

## 与 Tunnel 混用

如果想同时保留纯 IPv4 客户端访问，可以：

- `api.example.com` 用 AAAA 直连（主）
- `api4.example.com` 用 Tunnel（IPv4 兜底）

两者是不同主机名，互不冲突。

---

## 优点与局限

**优点**
- 零成本，不需要任何中转
- 速度只取决于家庭宽带上行
- 没有第三方依赖

**局限**
- 依赖运营商 IPv6 质量，部分地区和运营商 IPv6 路由很差
- 地址会变，必须配 DDNS
- 光猫防火墙是常见拦路虎
- 纯 IPv4 网络（部分办公网、老设备）无法访问
