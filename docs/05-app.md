# DVM 控制台 App

一个 WebView 壳，把「青龙 / 更新中心 / 状态仪表盘」装进手机，并为它们解决三件麻烦事：
**该连哪个地址、账号密码怎么记、两步验证怎么过。**

源码在 [`android/`](../android)。

---

## 一、它到底做了什么

| 能力 | 说明 |
|---|---|
| 双线路自动切换 | 每次打开并行探测内网与外网，取先通的那条 |
| 每入口独立地址 | 三个服务可以完全不同的域名、端口、路径前缀 |
| 凭据记忆 | AES-256-GCM + Android Keystore，只存本机、不随备份外泄 |
| 自动填充并提交 | 识别登录/令牌表单，填完自动点按钮 |
| TOTP 两步验证 | 内置 RFC 6238，自动算出 6 位码并提交 |
| 多令牌并存 | 更新中心与仪表盘令牌分开存，不会互相覆盖 |
| 文件选择器 | 青龙上传脚本、更新中心上传发布包都能用 |

---

## 二、地址是怎么拼出来的

这是最容易配错的地方，先看规则。

### 内网（同 Wi-Fi 直连服务）

**规则：`协议 + 主机 + 端口 + /`**

```
内网地址填：http://192.168.1.10
    青龙     → http://192.168.1.10:5700/
    更新中心 → http://192.168.1.10:3077/
    状态面板 → http://192.168.1.10:8765/
```

内网只填**主机**，端口由各入口自带。直连服务时路径都是根路径，所以内网会**忽略**「路径前缀」字段。

### 外网（走 Caddy 反代）

**规则：`外网地址 + 路径前缀`**

```
外网地址填：https://dash.example.com
    更新中心 → https://dash.example.com/ag2a/
    状态面板 → https://dash.example.com/
```

如果某个入口和主域名**不是同一个域名**（很常见：青龙单独一个 `ql.` 子域），
在「高级 → 自定义各入口地址」里给它单独填**外网地址**即可：

```
青龙外网地址：https://ql.example.com
```

### 一定要知道的坑

`https://dash.example.com/ag2a`（**没有结尾斜杠**）和 `/ag2a/` 是两个不同的东西。
Caddy 的 `handle_path /ag2a/*` 只匹配后者，前者会掉到默认反代上，通常表现为
返回一段 `{"error":"unauthorized"}` 或一个不相干的页面。

App 已经做了归一化：填 `https://dash.example.com/ag2a` 会自动补成带斜杠的版本。

---

## 三、凭据与令牌

| 字段 | 用途 | 存在哪 |
|---|---|---|
| 面板账号 / 密码 | 青龙等用户名密码表单 | 加密的 SharedPreferences |
| 各入口「访问令牌」 | 更新中心 `X-Console-Token`、仪表盘 Cookie 登录 | 同上，按入口分别保存 |
| 各入口「TOTP 密钥」 | 开启 2FA 的服务自动算码 | 同上，Base32 |

填充逻辑（按页面实际结构调整过的顺序）：

1. 页面有**令牌输入框**（`name=token` / `#tok` / placeholder 含「令牌」）
   → 只填该入口的令牌，**不会**把面板密码填进去
2. 否则页面有**密码框 + 用户名框** → 填账号密码
3. 出现**验证码输入框**（placeholder 含「位数字 / 验证码」）→ 填当前 TOTP 并提交

第 3 步是持续轮询的：青龙的两步验证页是 SPA 内部跳转，不会触发页面加载事件，
所以 App 每 1.5 秒检查一次页面有没有出现验证码框。

### 填充是「自愈」的，不是一次性

登录失败时前端往往会把表单**清空**（青龙在 2FA 被拒时就是如此）。如果填充只做一次，
之后用户看到的就是一个空表单——表现成「密码没自动填」。

所以 App 的规则是：

- 每次轮询**只在字段为空时**补，不会覆盖你自己输入的内容
- 提交失败后按 **8 秒节流**重试，表单被清空就能自动恢复
- 验证码的重试节流键包含码值本身，跨到下一个 30 秒窗口后自然再试

### 一个必须知道的限制：青龙的 2FA 有重放保护

青龙侧 `lastTwoFactorStep` 限制**同一 30 秒窗口只能成功验证一次**，重复提交返回 `450 未知错误`。
所以如果你刚在外网登录、30 秒内立刻切到内网再登一次，内网这次会被拒绝。

App 的处理：被拒后等下一个窗口自动重试（约 30 秒内自愈）。
不想等的话，切线路后稍停十几秒再打开即可。

> 提交时机还有个细节：如果当前 30 秒窗口只剩不到 6 秒，App 会等下一个窗口再提交，
> 避免「刚填完码就过期」。

### 令牌也可以「在网页里存一次」

`更新中心` 与 `状态仪表盘` 的令牌，除了填在 App 配置里，也可以直接在页面输入一次：
- 更新中心 → 存进 `localStorage`，之后不再问
- 状态面板 → 30 天 Cookie

App 里的令牌字段只是省掉这一次手输。

---

## 四、首次配置

1. 装好 APK，打开
2. 填**内网地址**（主机，不带端口，如 `http://192.168.1.10`）
3. 填**外网地址**（如 `https://dash.example.com`）
4. 填**面板账号 / 密码**
5. 展开「高级：自定义各入口地址」：
   - 青龙的外网地址（如果它和主域名不同）
   - 更新中心的访问令牌
   - 状态面板的访问令牌
   - 开了 2FA 的服务填 TOTP 密钥
6. 点「保存配置」→ 右上角出现「内网」或「外网」即配置成功

令牌在哪：

```bash
# 更新中心
cat /opt/ag2a-console/token

# 状态仪表盘
cat /opt/dashboard/token
```

---

## 五、调试用：配置注入

只有 **debug 包**支持，release 包不会读这个参数。

```bash
# 从文件注入（自动化测试 / 批量分发）
adb push cfg.json /data/local/tmp/cfg.json
adb shell run-as com.dvm.kit.debug cp /data/local/tmp/cfg.json files/cfg.json
adb shell am start -n com.dvm.kit.debug/com.dvm.kit.MainActivity \
  --es cfgfile /data/data/com.dvm.kit.debug/files/cfg.json
```

```bash
# 或直接内联
adb shell am start -n com.dvm.kit.debug/com.dvm.kit.MainActivity \
  --es dvmcfg '{"lan":"http://192.168.1.10","wan":"https://dash.example.com","user":"admin","pass":"***"}'
```

配置结构：

```json
{
  "lan": "http://192.168.1.10",
  "wan": "https://dash.example.com",
  "user": "admin",
  "pass": "your-password",
  "autofill": true,
  "services": [
    { "name": "青龙面板", "path": "",  "port": 5700, "icon": "QL",
      "color": "linear-gradient(135deg,#a855f7,#ec4899)",
      "lanUrl": "", "wanUrl": "https://ql.example.com", "token": "", "totp": "" },
    { "name": "更新中心", "path": "/ag2a/", "port": 3077, "icon": "PU",
      "color": "linear-gradient(135deg,#38bdf8,#6366f1)",
      "lanUrl": "", "wanUrl": "", "token": "<X-Console-Token>", "totp": "" },
    { "name": "状态面板", "path": "/", "port": 8765, "icon": "DV",
      "color": "linear-gradient(135deg,#34d399,#10b981)",
      "lanUrl": "", "wanUrl": "", "token": "<dashboard-token>", "totp": "" }
  ]
}
```

---

## 六、构建

```bash
cd android
export ANDROID_HOME=/path/to/android-sdk      # 需要 platform 34 + build-tools 34
./gradlew assembleDebug                       # 产物：app/build/outputs/apk/debug/
```

国内网络下 `sdkmanager` 拉清单容易失败，可以改为手动下载组件：

```
https://mirrors.cloud.tencent.com/AndroidSDK/platform-34-ext7_r03.zip        → platforms/android-34
https://mirrors.cloud.tencent.com/AndroidSDK/build-tools_r34-windows.zip      → build-tools/34.0.0
```

注意 build-tools 压缩包里目录名是 `android-14`，要改名为 `34.0.0`。

正式签名：在 `android/signing.properties` 里写 `DVM_STORE_FILE` / `DVM_STORE_PASSWORD` /
`DVM_KEY_ALIAS` / `DVM_KEY_PASSWORD`，然后 `./gradlew assembleRelease`。
不提供这些字段时会回退 debug 签名，保证任何人都能直接构建。

---

## 七、已知限制

- **自签证书**：面板用自签证书时，系统不信任就会白屏。用正式证书（Let's Encrypt）最省事
- **国产 ROM 的 ADB 安装限制**：ColorOS / HyperOS 会拦截 `adb install`（`-99` / `INSTALL_FAILED_USER_RESTRICTED`），
  用文件管理器点 APK 手动装即可
- **TOTP 密钥存在本机**：等于让 App 代你完成第二步。看重隔离的话，改用「访问令牌」字段
  （有效期通常 30–60 天），密码和 TOTP 都可以不填
- **内网地址只填主机**：写成 `http://192.168.1.10:5700` 也不会出错（端口会被各入口覆盖），
  但那样会让「内网忽略路径」的规则看起来费解，建议按规范写
