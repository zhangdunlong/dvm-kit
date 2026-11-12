package com.dvm.kit

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * 单 Activity WebView 容器：
 *  - 本地 home.html 作为导航与配置页（内网/外网双地址）
 *  - 远程页面加载完成后按需注入登录凭据
 *  - 内外网探测在后台线程做，不阻塞 UI
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var bar: ProgressBar
    private lateinit var prefs: Prefs
    private var fileCb: ValueCallback<Array<Uri>>? = null
    /** 当前打开的那个入口的专属令牌（不同服务令牌不同，随点击传入） */
    private var pendingToken: String = ""
    /** 当前入口的 TOTP 密钥（青龙等开启两步验证的服务） */
    private var pendingTotp: String = ""
    /** 配置缓存：自愈轮询每 1.5 秒跑一次，不必每次都过一遍 Keystore */
    private var cfgCache: String? = null
    /** 已提交过的验证码，跨页面切换也要去重：青龙同窗口重复提交会回 450 */
    private var lastOtpCode: String = ""
    private var lastOtpAt: Long = 0L
    /** 最近一次点击提交的时间：登录成功后页面会短暂重渲染出登录框，用它挡掉重复提交 */
    private var lastSubmitAt: Long = 0L
    private val ui = android.os.Handler(android.os.Looper.getMainLooper())

    private fun cfg(): JSONObject {
        val raw = cfgCache ?: prefs.load().also { cfgCache = it }
        return runCatching { JSONObject(raw) }.getOrNull() ?: JSONObject()
    }

    private val filePicker =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            val cb = fileCb ?: return@registerForActivityResult
            fileCb = null
            if (res.resultCode != RESULT_OK) {
                cb.onReceiveValue(null)
                return@registerForActivityResult
            }
            val d = res.data
            cb.onReceiveValue(
                when {
                    d == null -> null
                    d.clipData != null -> {
                        val cd = d.clipData!!
                        Array(cd.itemCount) { cd.getItemAt(it).uri }
                    }
                    d.data != null -> arrayOf(d.data!!)
                    else -> null
                }
            )
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)

        // 配置导入：
        //  - cfgfile 指向 App 自己的外部文件目录（/sdcard/Android/data/<pkg>/files/），
        //    该目录在分区存储下只有 adb 和 App 自己能写，别的应用写不进去，可用于离线备份/恢复
        //  - dvmcfg 内联 JSON 仅 debug 包支持，release 不接受，避免被任意 Intent 改写
        val file = intent?.getStringExtra("cfgfile")
        val inline = intent?.getStringExtra("dvmcfg")
        val raw = if (!file.isNullOrBlank()) {
            val root = getExternalFilesDir(null)?.canonicalPath
            val path = runCatching { java.io.File(file).canonicalPath }.getOrNull()
            if (root != null && path != null && path.startsWith(root)) {
                runCatching { java.io.File(path).readText() }.getOrNull()
            } else null
        } else if (BuildConfig.DEBUG) inline else null
        if (!raw.isNullOrBlank()) {
            runCatching { prefs.save(raw) }
            cfgCache = raw
            Log.i("DvmKit", "config imported")
        }

        val root = FrameLayout(this)
        web = WebView(this)
        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            visibility = View.GONE
        }
        root.addView(web, FrameLayout.LayoutParams(-1, -1))
        root.addView(bar, FrameLayout.LayoutParams(-1, dp(3)))
        setContentView(root)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = true
            allowContentAccess = true
            loadWithOverviewMode = true
            useWideViewPort = true
            // 内网多为 http://192.168.x，混合内容需放行，否则 https 页面里的 http 请求会被拦
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            cacheMode = WebSettings.LOAD_DEFAULT
            userAgentString = "$userAgentString DvmKit/${BuildConfig.VERSION_NAME}"
        }

        web.addJavascriptInterface(Bridge(), "DvmApp")

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                req: WebResourceRequest
            ): Boolean {
                val u = req.url.toString()
                if (u.startsWith("http://") || u.startsWith("https://") || u.startsWith("file://")) {
                    return false
                }
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(u))) }
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                bar.visibility = View.GONE
                if (url.startsWith("file://")) return
                // 立即补一次，后续交给每 1.5 秒的轮询自愈
                healForm()
                web.postDelayed({ healForm() }, 800)
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                bar.progress = newProgress
                bar.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
            }

            // 青龙上传脚本 / 导入备份需要文件选择器
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                fileCb?.onReceiveValue(null)
                fileCb = callback
                return try {
                    filePicker.launch(params.createIntent())
                    true
                } catch (e: Exception) {
                    fileCb = null
                    false
                }
            }
        }

        web.loadUrl(HOME)

        // 表单自愈轮询：补空字段、重试失败的提交、按窗口填两步验证码
        ui.postDelayed(formTick, 1500)
    }

    /**
     * 自愈式表单填充。
     *
     * 不再是「加载时填一次」：登录失败后表单会被前端清空（例如青龙 2FA 同一
     * 30 秒窗口内重复提交会返回 450 并被重置），一次性填充之后就再也不会补，
     * 用户看到的就是「密码没自动填」。这里改为每次轮询只在字段为空时补，
     * 并按 8 秒节流重试提交，让失败能自己恢复。
     */
    private fun healForm() {
        val cfg = cfg()
        if (!cfg.optBoolean("autofill", true)) return
        val user = cfg.optString("user")
        val pass = cfg.optString("pass")
        val token = pendingToken.ifBlank { cfg.optString("token") }
        if (user.isBlank() && pass.isBlank() && token.isBlank() && pendingTotp.isBlank()) return

        // 两步验证码：剩余有效期不足 6 秒就留空，等下一个窗口再填，避免刚提交就过期。
        // 同一个码只提交一次（跨页面切换也去重），否则会撞上青龙的重放保护返回 450。
        // 注意：这里只判断是否允许，不提前记账——真正的记账在 JS 里点击验证按钮那一刻
        // 通过 DvmApp.noteOtp() 回调，否则会在验证码框还没出现时就把码用掉。
        var code = ""
        if (pendingTotp.isNotBlank()) {
            val pair = runCatching { totp(pendingTotp) }.getOrNull()
            if (pair != null && pair.second >= 6) {
                val c = pair.first
                val blocked = c == lastOtpCode && System.currentTimeMillis() - lastOtpAt < 25_000L
                if (!blocked) code = c
            }
        }

        // 2.5 秒冷却：只挡点击，不挡填写；足够盖住登录成功后的短暂重渲染
        val allow = System.currentTimeMillis() - lastSubmitAt >= 2500L

        val js = """
            (function(){
              var U=${JSONObject.quote(user)}, P=${JSONObject.quote(pass)};
              var T=${JSONObject.quote(token)}, C=${JSONObject.quote(code)};
              var A=$allow;
              var now=Date.now();

              function setv(el,v){
                var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');
                if(d&&d.set){ d.set.call(el,v); } else { el.value=v; }
                el.dispatchEvent(new Event('input',{bubbles:true}));
                el.dispatchEvent(new Event('change',{bubbles:true}));
              }
              function findBtn(re){
                var bs=document.querySelectorAll('button, input[type=submit], input[type=button]');
                for(var i=0;i<bs.length;i++){
                  var tx=String(bs[i].innerText||bs[i].value||'').replace(/\s+/g,'');
                  if(tx.length>0 && tx.length<=8 && re.test(tx)) return bs[i];
                }
                return null;
              }
              // 同一 key 8 秒内只点一次；验证码的 key 含码值，换窗口后自然可重试
              function submit(key,re){
                if(!A) return;
                window.__dvmSent=window.__dvmSent||{};
                var last=window.__dvmSent[key]||0;
                if(now-last<8000) return;
                var b=findBtn(re);
                if(!b) return;
                window.__dvmSent[key]=now;
                try{ DvmApp.noteSubmit(); }catch(e){}
                b.click();
              }

              var RE_LOGIN=/^(登录|登入|进入|验证|确认|提交|确定|解锁)/;
              var TOK='input[name=token],#tok,input[placeholder*="令牌"],input[placeholder*="X-Console-Token"]';
              var OTP='input[placeholder*="位数字"],input[placeholder*="验证码"],input[autocomplete="one-time-code"]';

              // 1) 令牌页（更新中心 / 仪表盘）：只填令牌，绝不把面板密码填进去
              var tok=document.querySelector(TOK);
              if(tok){
                if(!T) return;
                if(!tok.value) setv(tok,T);
                if(tok.value===T) submit('tok',RE_LOGIN);
                return;
              }

              // 2) 两步验证页：通常只有验证码框、没有密码框
              var otp=document.querySelector(OTP);
              var pw=document.querySelector('input[type=password]');
              if(otp && !pw){
                if(!C) return;
                if(!otp.value) setv(otp,C);
                if(otp.value===C){
                  window.__dvmSent=window.__dvmSent||{};
                  var k='otp:'+C, last=window.__dvmSent[k]||0;
                  if(A && now-last>=8000){
                    var b=findBtn(RE_LOGIN);
                    if(b){
                      window.__dvmSent[k]=now;
                      try{ DvmApp.noteOtp(C); DvmApp.noteSubmit(); }catch(e){}
                      b.click();
                    }
                  }
                }
                return;
              }

              // 3) 账号密码页：用户名与密码各自独立补，缺一个不影响另一个
              if(!pw) return;
              if(P && !pw.value) setv(pw,P);
              var u=document.querySelector('input[name=username]') ||
                    document.querySelector('input[autocomplete=username]') ||
                    document.querySelector('input[id=username]') ||
                    document.querySelector('input[type=text]');
              if(U && u && !u.value) setv(u,U);
              if(pw.value===P && (!u || u.value===U)) submit('pw',RE_LOGIN);
            })();
        """.trimIndent()
        web.evaluateJavascript(js, null)
    }

    private fun ping(base: String): Boolean {
        if (base.isBlank()) return false
        return try {
            val conn = URL(base).openConnection() as HttpURLConnection
            conn.connectTimeout = 1500
            conn.readTimeout = 1500
            conn.requestMethod = "GET"
            conn.instanceFollowRedirects = false
            conn.setRequestProperty("User-Agent", "DvmKit")
            val code = conn.responseCode
            conn.disconnect()
            code in 200..499
        } catch (e: Exception) {
            false
        }
    }

    /** 依次探测，第一个通就返回 true（多入口时不必全部等待） */
    private fun anyOk(list: List<String>): Boolean {
        for (u in list) if (ping(u)) return true
        return false
    }

    private fun urlsOf(o: JSONObject, key: String): List<String> {
        val a = o.optJSONArray(key) ?: return emptyList()
        return (0 until a.length()).map { a.optString(it).trim() }.filter { it.isNotEmpty() }
    }

    /** 表单自愈：远程页面每 1.5 秒检查一次，空字段补上、失败的提交按节流重试 */
    private val formTick = object : Runnable {
        override fun run() {
            try {
                if (!web.url.orEmpty().startsWith("file://")) healForm()
            } catch (_: Exception) {
            }
            ui.postDelayed(this, 1500)
        }
    }

    /**
     * 计算 TOTP（RFC 6238，SHA1 / 6 位 / 30 秒）。
     * 返回 验证码 与 当前时间窗剩余秒数。
     */
    private fun totp(secret: String): Pair<String, Long> {
        val key = base32Decode(secret)
        val now = System.currentTimeMillis()
        val step = now / 30_000L
        val left = 30 - (now / 1000L) % 30
        val msg = java.nio.ByteBuffer.allocate(8).putLong(step).array()
        val mac = javax.crypto.Mac.getInstance("HmacSHA1")
        mac.init(javax.crypto.spec.SecretKeySpec(key, "HmacSHA1"))
        val h = mac.doFinal(msg)
        val off = h[h.size - 1].toInt() and 0x0f
        val bin = ((h[off].toInt() and 0x7f) shl 24) or
                ((h[off + 1].toInt() and 0xff) shl 16) or
                ((h[off + 2].toInt() and 0xff) shl 8) or
                (h[off + 3].toInt() and 0xff)
        return String.format(java.util.Locale.US, "%06d", bin % 1_000_000) to left
    }

    /** RFC 4648 Base32 解码（忽略空格、连字符与填充） */
    private fun base32Decode(s: String): ByteArray {
        val map = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        val clean = s.uppercase().replace(Regex("[^A-Z2-7]"), "")
        var buf = 0
        var bits = 0
        val out = java.io.ByteArrayOutputStream()
        for (ch in clean) {
            val v = map.indexOf(ch)
            if (v < 0) continue
            buf = (buf shl 5) or v
            bits += 5
            if (bits >= 8) {
                out.write((buf shr (bits - 8)) and 0xff)
                bits -= 8
            }
        }
        return out.toByteArray()
    }



    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (web.url?.startsWith("file://") != true && web.canGoBack()) {
                web.goBack()
                return true
            }
            if (web.url?.startsWith("file://") != true) {
                pendingToken = ""
                web.loadUrl(HOME)
                return true
            }
        }
        return super.onKeyDown(keyCode, event)
    }

    inner class Bridge {

        @JavascriptInterface
        fun getConfig(): String = cfgCache ?: prefs.load().also { cfgCache = it }

        @JavascriptInterface
        fun saveConfig(json: String): Boolean {
            prefs.save(json)
            cfgCache = json
            return true
        }

        @JavascriptInterface
        fun clearConfig() {
            prefs.clear()
            cfgCache = null
        }

        /**
         * 并行探测两条线路。入参 {"lan":[...],"wan":[...]}，
         * 每条线路依次尝试其入口地址，任一可用即视为该线路可达。
         * 结果回传 window.__probeResult(lanOk, wanOk)
         */
        @JavascriptInterface
        fun probeMany(json: String) {
            lifecycleScope.launch {
                val o = runCatching { JSONObject(json) }.getOrNull() ?: JSONObject()
                val lan = urlsOf(o, "lan")
                val wan = urlsOf(o, "wan")
                val (lanOk, wanOk) = withContext(Dispatchers.IO) {
                    val a = async { anyOk(lan) }
                    val b = async { anyOk(wan) }
                    a.await() to b.await()
                }
                web.evaluateJavascript(
                    "window.__probeResult && window.__probeResult($lanOk, $wanOk)", null
                )
            }
        }

        @JavascriptInterface
        fun open(url: String, token: String, totp: String) {
            val u = url.trim()
            if (u.isEmpty()) return
            pendingToken = token.trim()
            pendingTotp = totp.trim()
            runOnUiThread { web.loadUrl(u) }
        }

        @JavascriptInterface
        fun noteSubmit() {
            lastSubmitAt = System.currentTimeMillis()
        }

        /** 页面真的点了「验证」才记账，避免码还没用上就被去重逻辑挡住 */
        @JavascriptInterface
        fun noteOtp(code: String) {
            lastOtpCode = code
            lastOtpAt = System.currentTimeMillis()
        }

        @JavascriptInterface
        fun goHome() {
            pendingToken = ""
            pendingTotp = ""
            runOnUiThread { web.loadUrl(HOME) }
        }

        @JavascriptInterface
        fun currentUrl(): String = web.url ?: ""

        @JavascriptInterface
        fun toast(msg: String) {
            runOnUiThread { Toast.makeText(this@MainActivity, msg, Toast.LENGTH_SHORT).show() }
        }
    }

    companion object {
        private const val HOME = "file:///android_asset/home.html"
    }
}
