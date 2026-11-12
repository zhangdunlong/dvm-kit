package com.dvm.kit

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
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
                if (!url.startsWith("file://")) injectCredentials(view)
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
    }

    private fun injectCredentials(view: WebView) {
        val cfg = runCatching { JSONObject(prefs.load()) }.getOrNull() ?: return
        if (!cfg.optBoolean("autofill", true)) return
        val user = cfg.optString("user")
        val pass = cfg.optString("pass")
        if (user.isBlank() || pass.isBlank()) return

        val js = """
            (function(){
              if (window.__dvmFilled) return;
              var p = document.querySelector('input[type=password]');
              if (!p) return;
              var u = document.querySelector('input[name=username]') ||
                      document.querySelector('input[autocomplete=username]') ||
                      document.querySelector('input[type=text]');
              if (!u) return;
              u.value = ${JSONObject.quote(user)};
              p.value = ${JSONObject.quote(pass)};
              ['input','change'].forEach(function(t){
                u.dispatchEvent(new Event(t,{bubbles:true}));
                p.dispatchEvent(new Event(t,{bubbles:true}));
              });
              window.__dvmFilled = true;
            })();
        """.trimIndent()
        view.evaluateJavascript(js, null)
    }

    private fun ping(base: String): Boolean {
        if (base.isBlank()) return false
        return try {
            val conn = URL(base).openConnection() as HttpURLConnection
            conn.connectTimeout = 2500
            conn.readTimeout = 2500
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

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (web.url?.startsWith("file://") != true && web.canGoBack()) {
                web.goBack()
                return true
            }
            if (web.url?.startsWith("file://") != true) {
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

        /** 异步探测内网/外网连通性，结果回传 window.__probe(lanOk, wanOk) */
        @JavascriptInterface
        fun probe() {
            lifecycleScope.launch {
                val cfg = runCatching { JSONObject(prefs.load()) }.getOrNull() ?: JSONObject()
                val lan = cfg.optString("lan").trim().trimEnd('/')
                val wan = cfg.optString("wan").trim().trimEnd('/')
                val (lanOk, wanOk) = withContext(Dispatchers.IO) { ping(lan) to ping(wan) }
                web.evaluateJavascript(
                    "window.__probe && window.__probe($lanOk, $wanOk)", null
                )
            }
        }

        @JavascriptInterface
        fun openService(base: String, path: String) {
            val u = base.trim().trimEnd('/') + path
            runOnUiThread { web.loadUrl(u) }
        }

        @JavascriptInterface
        fun goHome() {
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
