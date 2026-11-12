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
    private val ui = android.os.Handler(android.os.Looper.getMainLooper())

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

        // 仅调试包：支持从 Intent 注入配置，便于自动化测试与批量部署
        if (BuildConfig.DEBUG) {
            val file = intent?.getStringExtra("cfgfile")
            val inline = intent?.getStringExtra("dvmcfg")
            val raw = if (!file.isNullOrBlank()) {
                runCatching { java.io.File(file).readText() }.getOrNull()
            } else inline
            if (!raw.isNullOrBlank()) {
                runCatching { prefs.save(raw) }
                Log.i("DvmKit", "config seeded from intent")
            }
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
                // SPA 登录框往往晚于 onPageFinished 渲染，补两次重试
                injectCredentials(view, url)
                web.postDelayed({ injectCredentials(view, url) }, 1200)
                web.postDelayed({ injectCredentials(view, url) }, 2800)
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

        // 两步验证：进入远程页面后定时检查是否有验证码输入框需要填
        ui.postDelayed(otpTick, 2000)
    }

    private fun injectCredentials(view: WebView, url: String) {
        val cfg = runCatching { JSONObject(prefs.load()) }.getOrNull() ?: return
        if (!cfg.optBoolean("autofill", true)) return
        val user = cfg.optString("user")
        val pass = cfg.optString("pass")
        val token = pendingToken.ifBlank { cfg.optString("token") }
        if (user.isBlank() && pass.isBlank() && token.isBlank()) return

        // 页面若存在令牌输入框（更新中心 / 仪表盘），只填令牌，绝不把面板密码填进去
        val js = """
            (function(){
              if (window.__dvmFilled) return;
              function setv(el,v){
                var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');
                if(d&&d.set){ d.set.call(el,v); } else { el.value=v; }
                el.dispatchEvent(new Event('input',{bubbles:true}));
                el.dispatchEvent(new Event('change',{bubbles:true}));
              }
              var tokSel = 'input[name=token],#tok,input[placeholder*="令牌"],input[placeholder*="X-Console-Token"]';
              var t = document.querySelector(tokSel);
              if (t) {
                var tv = ${JSONObject.quote(token)};
                if (tv) { setv(t, tv); window.__dvmFilled = true; maybeSubmit(); }
                return;
              }
              var p = document.querySelector('input[type=password]');
              if (!p) return;
              var u = document.querySelector('input[name=username]') ||
                      document.querySelector('input[autocomplete=username]') ||
                      document.querySelector('input[id=username]') ||
                      document.querySelector('input[type=text]');
              if (!u) return;
              setv(u, ${JSONObject.quote(user)});
              setv(p, ${JSONObject.quote(pass)});
              window.__dvmFilled = true;
              maybeSubmit();

              function maybeSubmit(){
                if (${JSONObject.quote(user)} === '' && ${JSONObject.quote(pass)} === '' &&
                    ${JSONObject.quote(token)} === '') return;
                if (window.__dvmClicked) return;
                var btn = null, bs = document.querySelectorAll('button');
                for (var i=0;i<bs.length;i++){
                  var tx = (bs[i].innerText || '').replace(/\s+/g,'');
                  if (/^(登录|登入|进入|验证|确认|提交|确定|解锁)/.test(tx) && tx.length <= 8) { btn = bs[i]; break; }
                }
                if (!btn) return;
                window.__dvmClicked = 1;
                setTimeout(function(){ btn.click(); }, 150);
              }
            })();
        """.trimIndent()
        view.evaluateJavascript(js, null)
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

    /** 两步验证：轮询页面是否出现验证码输入框，出现就填入当前 TOTP 并提交 */
    private val otpTick = object : Runnable {
        override fun run() {
            try {
                fillOtpIfNeeded()
            } catch (_: Exception) {
            }
            ui.postDelayed(this, 1500)
        }
    }

    private fun fillOtpIfNeeded() {
        if (pendingTotp.isBlank()) return
        if (web.url.orEmpty().startsWith("file://")) return
        val pair = runCatching { totp(pendingTotp) }.getOrNull() ?: return
        val code = pair.first
        // 剩余有效期不足 6 秒就别提交，等下一个窗口，避免刚提交就过期
        val ready = pair.second >= 6
        val js = """
            (function(){
              var code = ${JSONObject.quote(code)};
              var el = document.querySelector('input[placeholder*="位数字"]') ||
                       document.querySelector('input[placeholder*="验证码"]') ||
                       document.querySelector('input[autocomplete="one-time-code"]');
              if (!el) return;
              if (window.__dvmOtpVal === code) {
                if ($ready && !window.__dvmOtpSent) { window.__dvmOtpSent = 1; submit(); }
                return;
              }
              var d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
              if (d && d.set) { d.set.call(el, code); } else { el.value = code; }
              el.dispatchEvent(new Event('input', {bubbles:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              window.__dvmOtpVal = code;
              if ($ready && !window.__dvmOtpSent) { window.__dvmOtpSent = 1; submit(); }

              function submit(){
                var bs = document.querySelectorAll('button');
                for (var i=0;i<bs.length;i++){
                  var tx = (bs[i].innerText||'').replace(/\s+/g,'');
                  if (tx === '验证' || tx === '确定' || tx === '提交') { bs[i].click(); return; }
                }
              }
            })();
        """.trimIndent()
        web.evaluateJavascript(js, null)
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
        fun getConfig(): String = prefs.load()

        @JavascriptInterface
        fun saveConfig(json: String): Boolean {
            prefs.save(json)
            return true
        }

        @JavascriptInterface
        fun clearConfig() {
            prefs.clear()
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
