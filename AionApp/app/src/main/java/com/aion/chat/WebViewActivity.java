package com.aion.chat;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.ConsoleMessage;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.appcompat.app.AlertDialog;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

/**
 * WebView 全屏聊天页
 * - 支持 JS / DOM Storage / WebSocket
 * - 自动授予麦克风权限（给 Web 端 getUserMedia 用）
 * - 支持文件上传（图片/视频选择）
 */
public class WebViewActivity extends AppCompatActivity {

    private static final int REQ_AUDIO = 1001;
    private static final int REQ_CAMERA = 1002;
    private WebView webView;
    private String targetUrl;
    private boolean pageLoaded = false;
    private boolean permissionsRequested = false;
    private int retryCount = 0;
    private static final int MAX_RETRY = 5;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ValueCallback<Uri[]> fileCallback;
    private PermissionRequest pendingPermRequest;

    private final ActivityResultLauncher<Intent> fileChooserLauncher =
            registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
                if (fileCallback == null) return;
                Uri[] uris = null;
                if (result.getResultCode() == RESULT_OK && result.getData() != null) {
                    if (result.getData().getClipData() != null) {
                        int count = result.getData().getClipData().getItemCount();
                        uris = new Uri[count];
                        for (int i = 0; i < count; i++) {
                            uris[i] = result.getData().getClipData().getItemAt(i).getUri();
                        }
                    } else if (result.getData().getData() != null) {
                        uris = new Uri[]{result.getData().getData()};
                    }
                }
                fileCallback.onReceiveValue(uris);
                fileCallback = null;
            });

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // 状态栏/导航栏颜色与页面暖色背景一致
        getWindow().setStatusBarColor(0xFFfff9f5);
        getWindow().setNavigationBarColor(0xFFfff9f5);
        // 浅色背景需要深色图标
        int uiFlags = android.view.View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            uiFlags |= android.view.View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        getWindow().getDecorView().setSystemUiVisibility(uiFlags);

        // 开启 WebView 调试（Android Studio Logcat 可看 console.log）
        WebView.setWebContentsDebuggingEnabled(true);

        webView = new WebView(this);
        setContentView(webView);

        // 原生麦克风桥接（绕过 getUserMedia 的 HTTPS 限制）
        webView.addJavascriptInterface(new AudioBridge(webView), "AionAudio");

        // 原生摄像头桥接（绕过 getUserMedia 的 HTTPS 限制）
        webView.addJavascriptInterface(new CameraBridge(webView), "AionCamera");

        // 原生 BLE 桥接（绕过 WebView 不支持 Web Bluetooth API 的限制）
        webView.addJavascriptInterface(new BleBridge(webView, this), "AionBle");

        // 权限请求延迟到页面加载完成后，避免系统弹窗阻塞 WebView 加载
        // 见 onPageFinished → requestPermissionsSequentially()

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);               // localStorage
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false); // 允许自动播放音频（TTS / 闹铃）
        s.setAllowFileAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " AionChatApp/1.0");

        // 让 WebView 的渲染和真实 Chrome 保持一致
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            webView.getSettings().setSafeBrowsingEnabled(false);
        }

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String scheme = request.getUrl().getScheme();
                // 错误页按钮：重试 / 切换地址
                if ("aion".equals(scheme)) {
                    String host = request.getUrl().getHost();
                    if ("retry".equals(host)) {
                        retryCount = 0;
                        pageLoaded = false;
                        webView.loadUrl(targetUrl);
                    } else if ("switch".equals(host)) {
                        SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
                        prefs.edit().putBoolean("auto_connect", false).apply();
                        startActivity(new Intent(WebViewActivity.this, LauncherActivity.class));
                        finish();
                    }
                    return true;
                }
                // 站内导航留在 WebView，外部链接用浏览器打开
                String urlHost = request.getUrl().getHost();
                if (urlHost != null && (urlHost.contains("192.168.") || urlHost.contains("100.117.") || urlHost.contains("localhost") || urlHost.contains("127.0.0.1"))) {
                    return false;
                }
                startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                pageLoaded = false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                // 过滤掉错误页的 onPageFinished（data: URL）
                if (url != null && !url.startsWith("data:")) {
                    pageLoaded = true;
                    retryCount = 0;
                    // 页面加载成功后，延迟请求权限（串行，不阻塞页面）
                    if (!permissionsRequested) {
                        permissionsRequested = true;
                        mainHandler.postDelayed(() -> requestPermissionsSequentially(0), 1500);
                    }
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                // 只处理主页面加载失败（非子资源）
                if (request.isForMainFrame()) {
                    pageLoaded = false;
                    android.util.Log.e("AionWebView", "页面加载失败: " + error.getDescription());
                    showErrorPage(view, error.getDescription().toString());
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            // ── 麦克风+摄像头权限自动授予（给网页 getUserMedia 用） ──
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                String[] resources = request.getResources();
                boolean needAudio = false, needVideo = false;
                for (String res : resources) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(res)) needAudio = true;
                    if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(res)) needVideo = true;
                }
                if (!needAudio && !needVideo) { request.deny(); return; }

                // 检查所需的 Android 权限
                boolean hasAudio = !needAudio || ContextCompat.checkSelfPermission(
                        WebViewActivity.this, Manifest.permission.RECORD_AUDIO)
                        == PackageManager.PERMISSION_GRANTED;
                boolean hasVideo = !needVideo || ContextCompat.checkSelfPermission(
                        WebViewActivity.this, Manifest.permission.CAMERA)
                        == PackageManager.PERMISSION_GRANTED;

                if (hasAudio && hasVideo) {
                    request.grant(resources);
                } else {
                    // 存下来，等权限回调后再授予
                    pendingPermRequest = request;
                    java.util.List<String> missing = new java.util.ArrayList<>();
                    if (!hasAudio) missing.add(Manifest.permission.RECORD_AUDIO);
                    if (!hasVideo) missing.add(Manifest.permission.CAMERA);
                    ActivityCompat.requestPermissions(WebViewActivity.this,
                            missing.toArray(new String[0]), REQ_AUDIO);
                }
            }

            // ── 文件上传（图片/视频选择） ──
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                fileCallback = callback;
                Intent intent = params.createIntent();
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    fileChooserLauncher.launch(intent);
                } catch (Exception e) {
                    fileCallback = null;
                    Toast.makeText(WebViewActivity.this, "无法打开文件选择器", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }

            // ── 控制台日志（方便调试） ──
            @Override
            public boolean onConsoleMessage(ConsoleMessage msg) {
                android.util.Log.d("AionWebView",
                        msg.message() + " -- line " + msg.lineNumber() + " of " + msg.sourceId());
                return true;
            }
        });

        // 加载目标 URL
        targetUrl = getIntent().getStringExtra("url");
        if (targetUrl == null || targetUrl.isEmpty()) {
            targetUrl = "http://192.168.xx.xxx:8080/chat";
        }
        webView.loadUrl(targetUrl);
    }

    /**
     * 加载失败时显示错误页：自动重试 + 手动按钮
     */
    private void showErrorPage(WebView view, String errorMsg) {
        if (retryCount < MAX_RETRY) {
            retryCount++;
            int delay = Math.min(retryCount * 2000, 8000); // 2s, 4s, 6s, 8s, 8s
            android.util.Log.i("AionWebView", "自动重试 " + retryCount + "/" + MAX_RETRY + "，" + delay + "ms 后重试");
            String retryHtml = "<html><body style='background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;"
                    + "display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0;'>"
                    + "<div style='font-size:48px;margin-bottom:16px'>📡</div>"
                    + "<div style='font-size:16px;margin-bottom:8px'>正在连接服务器...</div>"
                    + "<div style='font-size:13px;color:#888;margin-bottom:4px'>第 " + retryCount + " 次重试（最多 " + MAX_RETRY + " 次）</div>"
                    + "<div style='font-size:12px;color:#666'>" + errorMsg + "</div>"
                    + "</body></html>";
            view.loadDataWithBaseURL(null, retryHtml, "text/html", "utf-8", null);
            mainHandler.postDelayed(() -> {
                if (webView != null && !pageLoaded) {
                    webView.loadUrl(targetUrl);
                }
            }, delay);
        } else {
            // 重试耗尽，显示手动操作页面
            String failHtml = "<html><body style='background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;"
                    + "display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0;'>"
                    + "<div style='font-size:48px;margin-bottom:16px'>😵</div>"
                    + "<div style='font-size:16px;margin-bottom:8px'>无法连接到服务器</div>"
                    + "<div style='font-size:13px;color:#888;margin-bottom:16px'>" + errorMsg + "</div>"
                    + "<div style='font-size:12px;color:#888;margin-bottom:20px'>" + targetUrl + "</div>"
                    + "<button onclick='window.location.href=\"aion://retry\"' style='padding:12px 32px;font-size:15px;"
                    + "border:none;border-radius:10px;background:#e07c5c;color:white;cursor:pointer;margin-bottom:10px'>🔄 重新连接</button>"
                    + "<button onclick='window.location.href=\"aion://switch\"' style='padding:10px 28px;font-size:14px;"
                    + "border:1px solid #555;border-radius:10px;background:transparent;color:#aaa;cursor:pointer'>切换地址</button>"
                    + "</body></html>";
            view.loadDataWithBaseURL(null, failHtml, "text/html", "utf-8", null);
        }
    }

    // ── 串行权限请求链：页面加载后依次请求，每次只弹一个 ──
    private static final int PERM_STEP_NOTIFICATION = 0;
    private static final int PERM_STEP_AUDIO = 1;
    private static final int PERM_STEP_CAMERA = 2;
    private static final int PERM_STEP_LOCATION = 3;
    private static final int PERM_STEP_BLUETOOTH = 4;
    private static final int PERM_STEP_BATTERY = 5;
    private static final int PERM_STEP_DONE = 6;
    private static final int REQ_BLUETOOTH = 4001;

    /**
     * 串行请求权限：step 0→通知, 1→麦克风, 2→定位, 3→蓝牙, 4→电池优化
     * 每一步完成后在 onRequestPermissionsResult 中调用下一步
     */
    private void requestPermissionsSequentially(int step) {
        if (step >= PERM_STEP_DONE) return;

        switch (step) {
            case PERM_STEP_NOTIFICATION:
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                        && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2001);
                    return; // 等回调
                }
                requestPermissionsSequentially(PERM_STEP_AUDIO);
                break;

            case PERM_STEP_AUDIO:
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
                    return;
                }
                requestPermissionsSequentially(PERM_STEP_CAMERA);
                break;

            case PERM_STEP_CAMERA:
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
                    return;
                }
                requestPermissionsSequentially(PERM_STEP_LOCATION);
                break;

            case PERM_STEP_LOCATION:
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{
                                    Manifest.permission.ACCESS_FINE_LOCATION,
                                    Manifest.permission.ACCESS_COARSE_LOCATION
                            }, REQ_LOCATION);
                    return;
                }
                // 前台定位已有，尝试后台定位
                requestBackgroundLocationOrNext();
                break;

            case PERM_STEP_BLUETOOTH:
                requestBluetoothOrNext();
                break;

            case PERM_STEP_BATTERY:
                requestBatteryOptimization();
                // 电池优化是 startActivity，没有回调，直接结束
                break;
        }
    }

    private void requestBackgroundLocationOrNext() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                && ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                new AlertDialog.Builder(this)
                        .setTitle("需要后台定位权限")
                        .setMessage("为了在后台持续上报位置信息，请在接下来的设置中选择「始终允许」")
                        .setPositiveButton("去设置", (d, w) -> {
                            ActivityCompat.requestPermissions(this,
                                    new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION},
                                    REQ_BACKGROUND_LOCATION);
                        })
                        .setNegativeButton("跳过", (d, w) -> {
                            requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
                        })
                        .show();
                return;
            } else {
                ActivityCompat.requestPermissions(this,
                        new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION},
                        REQ_BACKGROUND_LOCATION);
                return;
            }
        }
        requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
    }

    private void requestBluetoothOrNext() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            boolean needScan = ContextCompat.checkSelfPermission(this, "android.permission.BLUETOOTH_SCAN")
                    != PackageManager.PERMISSION_GRANTED;
            boolean needConnect = ContextCompat.checkSelfPermission(this, "android.permission.BLUETOOTH_CONNECT")
                    != PackageManager.PERMISSION_GRANTED;
            if (needScan || needConnect) {
                java.util.List<String> perms = new java.util.ArrayList<>();
                if (needScan) perms.add("android.permission.BLUETOOTH_SCAN");
                if (needConnect) perms.add("android.permission.BLUETOOTH_CONNECT");
                ActivityCompat.requestPermissions(this,
                        perms.toArray(new String[0]), REQ_BLUETOOTH);
                return;
            }
        }
        requestPermissionsSequentially(PERM_STEP_BATTERY);
    }

    // ── Android 权限回调：完成后继续下一步 ──
    @Override
    public void onRequestPermissionsResult(int code, @NonNull String[] perms, @NonNull int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == REQ_AUDIO && results.length > 0
                && results[0] == PackageManager.PERMISSION_GRANTED) {
            if (pendingPermRequest != null) {
                pendingPermRequest.grant(pendingPermRequest.getResources());
                pendingPermRequest = null;
            }
        }

        // 根据 requestCode 继续下一步
        switch (code) {
            case 2001: // POST_NOTIFICATIONS
                requestPermissionsSequentially(PERM_STEP_AUDIO);
                break;
            case REQ_AUDIO:
                requestPermissionsSequentially(PERM_STEP_CAMERA);
                break;
            case REQ_CAMERA:
                requestPermissionsSequentially(PERM_STEP_LOCATION);
                break;
            case REQ_LOCATION:
                if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                    requestBackgroundLocationOrNext();
                } else {
                    requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
                }
                break;
            case REQ_BACKGROUND_LOCATION:
                requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
                break;
            case REQ_BLUETOOTH:
                requestPermissionsSequentially(PERM_STEP_BATTERY);
                break;
        }
    }

    // ── 返回键 / 手势返回 ──
    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            // 已经退到最顶层，弹出选择对话框
            new AlertDialog.Builder(this, R.style.Theme_AionChat_Dialog)
                .setTitle("Aion Oloth")
                .setMessage("要切换连接地址还是退出？")
                .setPositiveButton("切换地址", (d, w) -> {
                    SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
                    prefs.edit().putBoolean("auto_connect", false).apply();
                    startActivity(new Intent(this, LauncherActivity.class));
                    finish();
                })
                .setNegativeButton("退出", (d, w) -> finish())
                .setNeutralButton("取消", null)
                .show();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 告诉推送服务：前台已打开，不需要弹通知
        notifyServiceForeground(true);
        // 回到前台：强制重连 WebSocket + 重新加载当天消息
        if (webView != null && pageLoaded) {
            webView.evaluateJavascript(
                "(function(){" +
                "  if(typeof ws!=='undefined' && ws.readyState!==1){" +
                "    console.log('[AionApp] WS断线，重连+刷新');" +
                "    connectWS();" +
                "    setTimeout(function(){if(typeof loadMessages==='function')loadMessages();},1500);" +
                "  }" +
                "})();",
                null);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        // 告诉推送服务：前台已关闭，需要弹通知
        notifyServiceForeground(false);
    }

    private void notifyServiceForeground(boolean active) {
        Intent intent = new Intent(this, AionPushService.class);
        intent.putExtra("action", "set_foreground");
        intent.putExtra("active", active);
        startService(intent);
    }

    private void requestBatteryOptimization() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            try {
                if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                    Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                }
            } catch (Exception e) {
                android.util.Log.w("AionWebView", "电池优化引导失败: " + e.getMessage());
            }
        }
    }

    private static final int REQ_LOCATION = 3001;
    private static final int REQ_BACKGROUND_LOCATION = 3002;

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacksAndMessages(null);
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
