# 安全清单

把服务暴露到公网之前，请逐条确认。这不是可选项——手机服务器一旦被拿下，
攻击者拿到的是你全部的家庭网络和账号。

---

## 一、先看清自己的暴露面

```bash
# 所有监听端口
ss -ltnp

# 只列对外（非 127.0.0.1）的
ss -ltnp | grep -vE '127\.0\.0\.1|\[::1\]'
```

**判定原则**：每一条对外监听都必须能回答「为什么它需要对外」。
回答不了的，改成只监听 `127.0.0.1`，由 Caddy 反代。

从公网实测（换一台不在内网的设备，或在手机上关 Wi-Fi 用流量）：

```bash
# 把地址换成你的公网 IP / 域名
for p in 80 443 3065 3077 5555 5700 7863 7890 8765 9090; do
  printf "%-6s " "$p"
  curl -s -o /dev/null -w "%{http_code}\n" --max-time 4 "http://[你的IPv6]:$p/"
done
```

**理想结果：只有 80 和 443 返回非 000。**

---

## 二、绝对不能暴露到公网的端口

| 端口 | 服务 | 为什么危险 |
|---|---|---|
| **7890** | Clash / Mihomo 混合代理 | 任何人不登录就能用你的节点，还能当跳板做坏事 |
| **9090** | Clash 控制 API | 无鉴权，可以直接改你的全部代理配置 |
| **5555** | ADB over TCP | 等于把 root shell 挂在公网上 |
| **3077** | 项目更新中心 | 能上传并替换服务的可执行文件 |
| **8765** | 状态仪表盘 | 泄露设备状态、内外网 IP |

Clash 默认配置是 `allow-lan: true` + `bind-address: "*"`，
**这意味着 7890 和 9090 会监听所有网卡**。修改 `config.yaml`：

```yaml
allow-lan: false
bind-address: "127.0.0.1"
external-controller: 127.0.0.1:9090
```

改完重启：

```bash
# box_for_root
sh /data/adb/box/scripts/box.service restart
# 或系统服务
systemctl restart clash
```

---

## 三、面板与任务系统

### 密码

不要用 `admin/admin`、`admin/123456` 这类。一个 20 位的随机密码记不住就存密码管理器。

### 两步验证（强烈建议）

青龙面板：

1. 登录 → 右上角头像 → 设置 → 两步验证
2. 扫描二维码，填入 6 位动态码启用

启用后，仅凭密码无法登录。

> 如果用的是浏览器扩展 / 脚本自动登录，需要它支持 TOTP。
> 本项目的 `青龙签到助手` 扩展 v1.2.0 已内置 TOTP 计算。

### 登录失败限速

青龙默认有失败计数与 IP 封禁。不要关掉它。

---

## 四、文件上传面

能被上传文件的地方，就是能被写可执行文件的地方。检查这几处：

| 位置 | 鉴权 | 额外防护 |
|---|---|---|
| 项目更新中心 `/api/upload` | `X-Console-Token` | 体积 1~120MB、sha256、ELF 校验、解包上限、磁盘配额 |
| 青龙 `/api/scripts` | 登录态 | — |
| Agent2API `/api/plugins/install` | 登录态 | — |

**上传后不会立即生效**：更新中心只把包存到 `work/downloads`，
必须再点「更新」才会替换二进制，替换前会做 ELF 头校验与体积校验。

**自查**：

```bash
# 无 token 应该 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://dash.example.com/ag2a/api/projects

# 超大 body 应该 413
head -c 2000000 /dev/zero | tr '\0' 'x' > /tmp/big.json
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Content-Type: application/json" --data-binary @/tmp/big.json \
  https://dash.example.com/ag2a/api/projects
```

---

## 五、HTTPS 与证书

- 证书自动续期要能跑通，否则 90 天后整站失效
- Caddy 的 `admin off` 会关闭本地管理接口，改配置需重启进程
- 如果证书总是续期失败，考虑加监控：本项目的仪表盘会显示每张证书的剩余天数

---

## 六、系统层

```bash
# 敏感文件权限检查
ls -l /path/to/.dvmkit/dvmkit.env        # 应该 600
ls -l /path/to/console/token             # 应该 600
ls -l ~/.cloudflared/*.json              # 应该 600

# 修复
chmod 600 .dvmkit/dvmkit.env console/token
```

- `dvmkit.env` 里存着 `CF_API_TOKEN`，权限必须是 600
- 隧道凭据 `*.json` 等同密码
- 不要把这些文件提交到 Git

---

## 七、定期检查

建议每季度跑一次：

```bash
# 1. 对外端口有没有变多
ss -ltnp | grep -vE '127\.0\.0\.1|\[::1\]'

# 2. 证书剩余天数（仪表盘也能看）
for f in /var/lib/caddy/caddy/certificates/*/*/*.crt; do
  echo "$f: $(openssl x509 -in "$f" -noout -enddate)"
done

# 3. 面板登录日志有无异常 IP
# 青龙：面板 → 日志 → 登录日志

# 4. 系统更新
apt list --upgradable 2>/dev/null | head
```

---

## 八、出事之后

如果真的被入侵了：

1. **先断网**（关 Wi-Fi / 拔卡），别急着关机
2. 改所有相关密码（面板、CF、域名、账号）
3. 吊销 CF API Token 与隧道凭据，重新创建
4. 检查有没有被加计划任务 / 开机自启：
   ```bash
   crontab -l
   ls /etc/cron.* /etc/init.d /data/adb/service.d 2>/dev/null
   ```
5. 重装系统，不要试图「清理干净」——你不知道留下了什么

---

## 一句话总结

**默认拒绝，按需开放。**
每开一个端口，都要能说出它为什么必须对外。
