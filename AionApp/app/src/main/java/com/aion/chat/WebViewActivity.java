package com.aion.chat;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.os.PowerManager;
import android.provider.Settings;
import android.media.projection.MediaProjectionManager;
import android.content.ContentValues;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.ConsoleMessage;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.InputStream;
import java.io.IOException;

import androidx.activity.result.ActivityResultLauncher;
import androidx.appcompat.app.AlertDialog;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.aion.chat.supervision.AppSupervisionBridge;
import com.aion.chat.supervision.AppSupervisionRuntime;
import com.aion.chat.infrared.HugPillowInfraredBridge;
import androidx.core.view.WindowCompat;

/**
 * WebView 全屏聊天页
 * - 支持 JS / DOM Storage / WebSocket
 * - 自动授予麦克风权限（给 Web 端 getUserMedia 用）
 * - 支持文件上传（图片/视频选择）
 */
public class WebViewActivity extends AppCompatActivity
        implements PhoneCameraPreviewCoordinator.Client {

    private static final int REQ_AUDIO = 1001;
    private static final int REQ_CAMERA = 1002;
    private WebView webView;
    private SharedAssetCache sharedAssetCache;
    private MediaCacheStore mediaCacheStore;
    private SharedJsonStore sharedJsonStore;
    private AionRingBleBridge ringBleBridge;
    private AionMiBandBleBridge miBandBleBridge;
    private CameraBridge cameraBridge;
    private PhoneCameraBridge phoneCameraBridge;
    private String targetUrl;
    private boolean initialPageLoadStarted = false;
    private boolean pageLoaded = false;
    private boolean activityResumed = false;
    private boolean permissionsRequested = false;
    private int retryCount = 0;
    private static final int MAX_RETRY = 5;
    private static final long CLIENT_ASSET_CHECK_INTERVAL_MS = 30L * 60L * 1000L;
    private static final String CLIENT_ASSET_LAST_CHECK = "client_asset_last_check";
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private boolean clientAssetRefreshInFlight = false;
    private boolean pendingClientUpdateReady = false;
    private boolean cloudflareAuthManifestRetried = false;
    private final CloudflareReauthState cloudflareReauthState = new CloudflareReauthState();
    private ValueCallback<Uri[]> fileCallback;
    private PermissionRequest pendingPermRequest;
    private static final String CLOUDFLARE_HOST = ConnectionEndpoint.CLOUDFLARE_HOST;
    private static final String CLOUDFLARE_ACCESS_HOST = "cloudflareaccess.com";

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

    private final ActivityResultLauncher<Intent> phoneScreenLauncher =
            registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
                if (result.getResultCode() == RESULT_OK && result.getData() != null) {
                    Intent serviceIntent = new Intent(this, AionPushService.class);
                    serviceIntent.putExtra("action", AionPushService.ACTION_START_PHONE_SCREEN);
                    serviceIntent.putExtra(AionPushService.EXTRA_RESULT_CODE, result.getResultCode());
                    serviceIntent.putExtra(AionPushService.EXTRA_RESULT_DATA, result.getData());
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        startForegroundService(serviceIntent);
                    } else {
                        startService(serviceIntent);
                    }
                    getSharedPreferences("aion_prefs", MODE_PRIVATE)
                            .edit().putBoolean("phone_screen_supervision", true).apply();
                    Toast.makeText(this, "手机屏幕监督已开启", Toast.LENGTH_SHORT).show();
                } else {
                    Toast.makeText(this, "未开启手机屏幕监督", Toast.LENGTH_SHORT).show();
                }
            });

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        SharedPreferences launchPrefs =
                getSharedPreferences("aion_prefs", MODE_PRIVATE);
        String launchUrl = WebViewLaunchPolicy.resolveUrl(
                getIntent().getStringExtra("url"),
                launchPrefs.getBoolean("auto_connect", false),
                launchPrefs.getString(
                        "saved_url", "http://192.168.xx.xxx:8080/chat"));
        if (launchUrl == null) {
            Intent launcherIntent = new Intent(this, LauncherActivity.class);
            launcherIntent.putExtra(
                    LauncherActivity.EXTRA_FORCE_ADDRESS_PICKER, true);
            startActivity(launcherIntent);
            finish();
            return;
        }
        targetUrl = ConnectionEndpoint.normalizePageUrl(launchUrl);
        if (targetUrl == null || targetUrl.isEmpty()) {
            targetUrl = "http://192.168.xx.xxx:8080/chat";
        }
        startPushService(targetUrl);

        // WebView 始终 edge-to-edge；子页面的状态栏避让由 chat.html 的 iframe 浮层处理
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        getWindow().setStatusBarColor(android.graphics.Color.TRANSPARENT);
        getWindow().setNavigationBarColor(android.graphics.Color.TRANSPARENT);

        // 开启 WebView 调试（Android Studio Logcat 可看 console.log）
        WebView.setWebContentsDebuggingEnabled(true);

        webView = new WebView(this);
        sharedAssetCache = new SharedAssetCache(this);
        mediaCacheStore = new MediaCacheStore(this);
        sharedJsonStore = new SharedJsonStore(this);
        webView.setBackgroundColor(android.graphics.Color.TRANSPARENT);
        setContentView(webView);

        AppSupervisionRuntime.start(this);
        webView.addJavascriptInterface(
                new AppSupervisionBridge(this, AppSupervisionRuntime.get()),
                "AionAppSupervision");

        // 状态栏图标样式桥接（让网页可以根据主题动态切换深色/浅色图标）
        webView.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void setBarStyle(String style) {
                mainHandler.post(() -> {
                    android.view.View dv = getWindow().getDecorView();
                    int flags = dv.getSystemUiVisibility();
                    if ("light".equals(style)) {
                        // 浅色背景 → 深色图标
                        flags |= android.view.View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            flags |= android.view.View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                        }
                    } else {
                        // 深色背景 → 浅色图标
                        flags &= ~android.view.View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            flags &= ~android.view.View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                        }
                    }
                    dv.setSystemUiVisibility(flags);
                });
            }
        }, "AionStatusBar");

        // 手机屏幕监督桥接：用户显式授权后，后台服务可在监控提示音后抓取一帧屏幕。
        webView.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void requestPermission() {
                mainHandler.post(() -> {
                    MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
                    if (mpm == null) {
                        Toast.makeText(WebViewActivity.this, "此设备不支持屏幕捕获", Toast.LENGTH_SHORT).show();
                        return;
                    }
                    phoneScreenLauncher.launch(mpm.createScreenCaptureIntent());
                });
            }

            @JavascriptInterface
            public void stop() {
                Intent serviceIntent = new Intent(WebViewActivity.this, AionPushService.class);
                serviceIntent.putExtra("action", AionPushService.ACTION_STOP_PHONE_SCREEN);
                startService(serviceIntent);
                getSharedPreferences("aion_prefs", MODE_PRIVATE)
                        .edit().putBoolean("phone_screen_supervision", false).apply();
                mainHandler.post(() -> Toast.makeText(WebViewActivity.this, "手机屏幕监督已关闭", Toast.LENGTH_SHORT).show());
            }

            @JavascriptInterface
            public boolean isEnabled() {
                return getSharedPreferences("aion_prefs", MODE_PRIVATE)
                        .getBoolean("phone_screen_supervision", false);
            }

            @JavascriptInterface
            public void openAccessibilitySettings() {
                mainHandler.post(() -> {
                    Intent intent = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
                    startActivity(intent);
                    Toast.makeText(WebViewActivity.this,
                            "请在无障碍里开启 " + getString(R.string.app_name),
                            Toast.LENGTH_LONG).show();
                });
            }

            @JavascriptInterface
            public boolean isAccessibilityEnabled() {
                return isAionAccessibilityEnabled();
            }

            @JavascriptInterface
            public void testAccessibilityCapture() {
                Intent serviceIntent = new Intent(WebViewActivity.this, AionPushService.class);
                serviceIntent.putExtra("action", AionPushService.ACTION_TEST_ACCESSIBILITY_SCREEN);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    startForegroundService(serviceIntent);
                } else {
                    startService(serviceIntent);
                }
                mainHandler.post(() -> Toast.makeText(WebViewActivity.this, "已触发无障碍截图测试", Toast.LENGTH_SHORT).show());
            }
        }, "AionPhoneScreen");

        // 原生麦克风桥接（绕过 getUserMedia 的 HTTPS 限制）
        AudioBridge audioBridge = new AudioBridge(webView);
        webView.addJavascriptInterface(audioBridge, "AionAudio");

        // 原生摄像头桥接（绕过 getUserMedia 的 HTTPS 限制）
        cameraBridge = new CameraBridge(webView);
        webView.addJavascriptInterface(cameraBridge, "AionCamera");
        phoneCameraBridge = new PhoneCameraBridge(this);
        webView.addJavascriptInterface(phoneCameraBridge, "AionPhoneCamera");
        PhoneCameraPreviewCoordinator.shared().register(this);

        webView.addJavascriptInterface(
                new HugPillowInfraredBridge(this),
                "AionInfrared");

        // 原生视频录制桥接（复用摄像头+麦克风帧，MediaCodec+MediaMuxer 编码 MP4）
        VideoBridge videoBridge = new VideoBridge(webView, getCacheDir());
        audioBridge.setVideoBridge(videoBridge);
        cameraBridge.setVideoBridge(videoBridge);
        webView.addJavascriptInterface(videoBridge, "AionVideo");

        // 原生 BLE 桥接（绕过 WebView 不支持 Web Bluetooth API 的限制）
        webView.addJavascriptInterface(new BleBridge(webView, this), "AionBle");
        ringBleBridge = new AionRingBleBridge(webView, this);
        webView.addJavascriptInterface(ringBleBridge, "AionRingBle");
        miBandBleBridge = new AionMiBandBleBridge(webView, this);
        webView.addJavascriptInterface(miBandBleBridge, "AionMiBand");

        // 图片保存桥接（WebView 不支持 blob URL 下载，用原生方法写入相册）
        webView.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void save(String base64Data, String filename) {
                try {
                    byte[] bytes = Base64.decode(base64Data, Base64.DEFAULT);
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Images.Media.DISPLAY_NAME, filename);
                    values.put(MediaStore.Images.Media.MIME_TYPE, "image/png");
                    values.put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/Aion");
                    Uri uri = getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
                    if (uri != null) {
                        java.io.OutputStream os = getContentResolver().openOutputStream(uri);
                        if (os != null) { os.write(bytes); os.close(); }
                    }
                    mainHandler.post(() -> Toast.makeText(WebViewActivity.this, "图片已保存到相册", Toast.LENGTH_SHORT).show());
                } catch (Exception e) {
                    mainHandler.post(() -> Toast.makeText(WebViewActivity.this, "保存失败: " + e.getMessage(), Toast.LENGTH_SHORT).show());
                }
            }
        }, "AionImageSaver");

        webView.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void open(String url) {
                mainHandler.post(() -> handleNewWindowUrl(url));
            }
        }, "AionExternal");

        // Bounded replay cache controls. The settings page may call these
        // through the top-level WebView bridge without involving the server.
        webView.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public String getInfo() {
                return mediaCacheStore.getInfo().toString();
            }

            @JavascriptInterface
            public void setLimitMb(int limitMb) {
                mediaCacheStore.setLimitMb(limitMb);
            }

            @JavascriptInterface
            public void clear() {
                mediaCacheStore.clear();
            }
        }, "AppMediaCache");

        // Small JSON snapshots shared by every connection origin. This avoids
        // localStorage's LAN/Tailscale/Cloudflare origin isolation.
        webView.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public String get(String key) {
                return sharedJsonStore.get(key);
            }

            @JavascriptInterface
            public void put(String key, String value) {
                sharedJsonStore.put(key, value);
            }
        }, "AppSharedData");

        // 权限请求延迟到页面加载完成后，避免系统弹窗阻塞 WebView 加载
        // 见 onPageFinished → requestPermissionsSequentially()

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);               // localStorage
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false); // 允许自动播放音频（TTS / 闹铃）
        s.setAllowFileAccess(true);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setSupportMultipleWindows(true);
        // HTTPS/Cloudflare 页面禁止降级加载 HTTP 子资源；纯 HTTP 的 LAN/Tailscale
        // 顶层页面不属于 mixed content，因此两条私网线路不受影响。
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        // Allow WebView to revalidate normal responses. Verified frontend and
        // visual assets use SharedAssetCache, which is independent of hostname.
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUserAgentString(s.getUserAgentString() + " AionChatApp/1.0");

        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cookieManager.setAcceptThirdPartyCookies(webView, true);
        }

        // 让 WebView 的渲染和真实 Chrome 保持一致
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            webView.getSettings().setSafeBrowsingEnabled(true);
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
                        Intent launcherIntent =
                                new Intent(WebViewActivity.this, LauncherActivity.class);
                        launcherIntent.putExtra(
                                LauncherActivity.EXTRA_FORCE_ADDRESS_PICKER, true);
                        startActivity(launcherIntent);
                        finish();
                    }
                    return true;
                }
                return handleExternalNavigation(request.getUrl());
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (cloudflareReauthState.shouldBypass(
                        targetUrl, request.getUrl().toString(), request.isForMainFrame())) {
                    android.util.Log.i("AionWebView",
                            "Bypassing cached Cloudflare document for Access authentication");
                    return super.shouldInterceptRequest(view, request);
                }
                String path = request.getUrl().getPath();
                String host = request.getUrl().getHost();
                if (path == null) return super.shouldInterceptRequest(view, request);
                if (!isAionContentHost(host)) return super.shouldInterceptRequest(view, request);
                WebResourceResponse cached = sharedAssetCache.intercept(
                        request.getUrl(), request.getRequestHeaders());
                if (cached != null) return cached;
                WebResourceResponse media = mediaCacheStore.intercept(request);
                if (media != null) return media;
                return super.shouldInterceptRequest(view, request);
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
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                        CookieManager.getInstance().flush();
                    }
                    notifyCloudflareAuthReady(url);
                    runForegroundResumeSync();
                    if (activityResumed) {
                        view.evaluateJavascript(
                                "window.onAionAppForegroundChanged?.(true)", null);
                    }
                    notifyClientUpdateReady();
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

            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture,
                                          Message resultMsg) {
                WebView.HitTestResult hit = view.getHitTestResult();
                String hitUrl = hit != null ? hit.getExtra() : null;
                if (handleNewWindowUrl(hitUrl)) {
                    return false;
                }

                WebView popup = new WebView(WebViewActivity.this);
                popup.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView popupView, WebResourceRequest request) {
                        handleNewWindowUrl(request.getUrl() == null ? null : request.getUrl().toString());
                        popupView.destroy();
                        return true;
                    }

                    @SuppressWarnings("deprecation")
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView popupView, String url) {
                        handleNewWindowUrl(url);
                        popupView.destroy();
                        return true;
                    }

                    @Override
                    public void onPageStarted(WebView popupView, String url, Bitmap favicon) {
                        if (handleNewWindowUrl(url)) {
                            popupView.stopLoading();
                            popupView.destroy();
                        }
                    }
                });
                WebView.WebViewTransport transport = (WebView.WebViewTransport) resultMsg.obj;
                transport.setWebView(popup);
                resultMsg.sendToTarget();
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
        // Cached active documents render immediately. Version discovery and
        // staging happen in the background and never hold the first frame.
        loadTargetUrlOnce();
        maybeRefreshClientAssets(true);
    }

    private void loadTargetUrlOnce() {
        if (initialPageLoadStarted || webView == null) return;
        initialPageLoadStarted = true;
        sharedAssetCache.freezeForPageLoad();
        webView.loadUrl(targetUrl);
    }

    private boolean isAionContentHost(String host) {
        return ConnectionEndpoint.isAllowedContentHost(host, targetUrl);
    }

    private boolean isAllowedInWebViewHost(String host) {
        return isAionContentHost(host)
                || isCloudflareAccessHost(host);
    }

    private boolean handleExternalNavigation(Uri uri) {
        if (uri == null) return false;
        if (isAllowedInWebViewHost(uri.getHost())) {
            return false;
        }
        openExternalUrl(uri);
        return true;
    }

    private boolean handleNewWindowUrl(String url) {
        if (url == null || url.trim().isEmpty()) return false;
        Uri uri;
        try {
            uri = Uri.parse(url);
        } catch (Exception e) {
            return false;
        }
        if (isAllowedInWebViewHost(uri.getHost())) {
            webView.loadUrl(uri.toString());
        } else {
            openExternalUrl(uri);
        }
        return true;
    }

    private void openExternalUrl(Uri uri) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, uri);
            intent.addCategory(Intent.CATEGORY_BROWSABLE);
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "没有可打开此链接的应用", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "无法打开链接", Toast.LENGTH_SHORT).show();
        }
    }

    private boolean isCloudflareAccessHost(String host) {
        return host != null
                && (CLOUDFLARE_ACCESS_HOST.equalsIgnoreCase(host)
                || host.toLowerCase(java.util.Locale.ROOT)
                        .endsWith("." + CLOUDFLARE_ACCESS_HOST));
    }

    private boolean isAionAccessibilityEnabled() {
        return AionAccessibilityService.isEnabledInSettings(this);
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
    private static final int PERM_STEP_ACTIVITY_RECOGNITION = 4;
    private static final int PERM_STEP_BLUETOOTH = 5;
    private static final int PERM_STEP_BATTERY = 6;
    private static final int PERM_STEP_DONE = 7;
    private static final int REQ_BLUETOOTH = 4001;
    private static final int REQ_ACTIVITY_RECOGNITION = 4002;

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

            case PERM_STEP_ACTIVITY_RECOGNITION:
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                        && ContextCompat.checkSelfPermission(this, Manifest.permission.ACTIVITY_RECOGNITION)
                        != PackageManager.PERMISSION_GRANTED) {
                    ActivityCompat.requestPermissions(this,
                            new String[]{Manifest.permission.ACTIVITY_RECOGNITION}, REQ_ACTIVITY_RECOGNITION);
                    return;
                }
                requestPermissionsSequentially(PERM_STEP_BLUETOOTH);
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
                            requestPermissionsSequentially(PERM_STEP_ACTIVITY_RECOGNITION);
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
        requestPermissionsSequentially(PERM_STEP_ACTIVITY_RECOGNITION);
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
                requestPermissionsSequentially(PERM_STEP_ACTIVITY_RECOGNITION);
                break;
            case REQ_ACTIVITY_RECOGNITION:
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
        // 统一由 JS 判断当前页面状态，决定导航到 Home 还是弹对话框
        webView.evaluateJavascript(
            "(function(){ if(typeof handleNativeBack==='function') return handleNativeBack(); return 'dialog'; })()",
            value -> runOnUiThread(() -> {
                if ("\"dialog\"".equals(value)) {
                    showExitDialog();
                }
                // "handled" = JS 已导航到 Home，无需额外操作
            })
        );
    }

    private void showExitDialog() {
        new AlertDialog.Builder(this, R.style.Theme_AionChat_Dialog)
            .setTitle(getString(R.string.app_name))
            .setMessage("要切换连接地址还是退出？")
            .setPositiveButton("切换地址", (d, w) -> {
                SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
                prefs.edit().putBoolean("auto_connect", false).apply();
                Intent launcherIntent = new Intent(this, LauncherActivity.class);
                launcherIntent.putExtra(
                        LauncherActivity.EXTRA_FORCE_ADDRESS_PICKER, true);
                startActivity(launcherIntent);
                finish();
            })
            .setNegativeButton("退出", (d, w) -> finish())
            .setNeutralButton("取消", null)
            .show();
    }

    @Override
    protected void onResume() {
        super.onResume();
        AppSupervisionRuntime runtime = AppSupervisionRuntime.get();
        if (runtime != null) runtime.onAionsHomeForegroundChanged(true);
        activityResumed = true;
        setNativeCameraForeground(true);
        // 告诉推送服务：前台已打开，不需要弹通知
        notifyServiceForeground(true);
        if (ringBleBridge != null) {
            ringBleBridge.resumeHealthPageConnection();
        }
        // 回到前台：重连 WebSocket（如需要），并补拉当前会话，避免 WebView 后台冻结漏消息。
        if (webView != null && pageLoaded) {
            webView.evaluateJavascript(ForegroundResumeSyncScript.build(), null);
            webView.evaluateJavascript(
                    "window.onAionAppForegroundChanged?.(true)", null);
        }
        maybeRefreshClientAssets(false);
    }

    @Override
    protected void onPause() {
        super.onPause();
        AppSupervisionRuntime runtime = AppSupervisionRuntime.get();
        if (runtime != null) runtime.onAionsHomeForegroundChanged(false);
        activityResumed = false;
        setNativeCameraForeground(false);
        if (webView != null) {
            webView.evaluateJavascript(
                    "window.onAionAppForegroundChanged?.(false)", null);
        }
        releasePhoneCameraPreview();
        if (ringBleBridge != null) {
            ringBleBridge.releaseForBackgroundSync();
        }
        // 告诉推送服务：前台已关闭，需要弹通知
        notifyServiceForeground(false);
    }

    @Override
    public void pauseForEvent(Runnable released) {
        runOnUiThread(() -> {
            try {
                releasePhoneCameraMonitorPreview();
                if (webView != null) {
                    webView.evaluateJavascript(
                            "window.onAionPhoneCameraCaptureState?.(true)", null);
                }
            } finally {
                released.run();
            }
        });
    }

    @Override
    public void resumeAfterEvent() {
        runOnUiThread(() -> {
            if (webView != null) {
                webView.evaluateJavascript(
                        "window.onAionPhoneCameraCaptureState?.(false)", null);
            }
        });
    }

    private void releasePhoneCameraPreview() {
        if (cameraBridge != null) cameraBridge.stop();
        if (phoneCameraBridge != null) phoneCameraBridge.pausePreview();
    }

    private void setNativeCameraForeground(boolean foreground) {
        if (cameraBridge != null) cameraBridge.setAppForeground(foreground);
        if (phoneCameraBridge != null) {
            phoneCameraBridge.setAppForeground(foreground);
        }
    }

    private void releasePhoneCameraMonitorPreview() {
        if (phoneCameraBridge == null || !phoneCameraBridge.isPreviewVisible()) return;
        releasePhoneCameraPreview();
    }

    private void runForegroundResumeSync() {
        if (webView != null && pageLoaded) {
            webView.evaluateJavascript(ForegroundResumeSyncScript.build(), null);
        }
    }

    private void notifyServiceForeground(boolean active) {
        Intent intent = new Intent(this, AionPushService.class);
        intent.putExtra("action", PushServiceStartPolicy.ACTION_SET_FOREGROUND);
        intent.putExtra("active", active);
        if (targetUrl != null) intent.putExtra("url", targetUrl);
        startService(intent);
    }

    private void startPushService(String url) {
        Intent serviceIntent = new Intent(this, AionPushService.class);
        serviceIntent.putExtra("url", url);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }

    private void notifyCloudflareAuthReady(String pageUrl) {
        try {
            Uri uri = Uri.parse(pageUrl);
            if (!ConnectionEndpoint.isCloudflareHost(uri.getHost())) return;
            String cookie = CookieManager.getInstance()
                    .getCookie(ConnectionEndpoint.CLOUDFLARE_COOKIE_URL);
            if (!ConnectionEndpoint.hasCloudflareAccessCookie(cookie)) return;
            cloudflareReauthState.authenticationCompleted();
            Intent intent = new Intent(this, AionPushService.class);
            intent.putExtra("action", AionPushService.ACTION_REFRESH_CLOUDFLARE_AUTH);
            intent.putExtra("url", pageUrl);
            startService(intent);
            // Retry at most once if the cold-start check raced the Access cookie.
            if (!cloudflareAuthManifestRetried) {
                cloudflareAuthManifestRetried = true;
                mainHandler.postDelayed(() -> maybeRefreshClientAssets(true), 1000);
            }
        } catch (Exception e) {
            android.util.Log.w("AionWebView", "Cloudflare auth sync failed: "
                    + e.getClass().getSimpleName());
        }
    }

    private void maybeRefreshClientAssets(boolean force) {
        if (sharedAssetCache == null || targetUrl == null || targetUrl.isEmpty()
                || clientAssetRefreshInFlight) return;
        SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
        long now = System.currentTimeMillis();
        long last = prefs.getLong(CLIENT_ASSET_LAST_CHECK, 0L);
        if (!force && now - last < CLIENT_ASSET_CHECK_INTERVAL_MS) return;
        prefs.edit().putLong(CLIENT_ASSET_LAST_CHECK, now).apply();
        clientAssetRefreshInFlight = true;
        sharedAssetCache.refreshManifest(targetUrl, result -> mainHandler.post(() -> {
            clientAssetRefreshInFlight = false;
            if (result == SharedAssetCache.RefreshResult.AUTH_REQUIRED) {
                beginCloudflareReauthentication();
                return;
            }
            if (result == SharedAssetCache.RefreshResult.REFRESHED) {
                pendingClientUpdateReady = true;
                notifyClientUpdateReady();
            }
        }));
    }

    private void beginCloudflareReauthentication() {
        if (webView == null || !cloudflareReauthState.begin(targetUrl)) return;
        android.util.Log.i("AionWebView", "Opening Cloudflare Access authentication");
        pageLoaded = false;
        webView.loadUrl(targetUrl);
    }

    private void notifyClientUpdateReady() {
        if (!pendingClientUpdateReady || webView == null || !pageLoaded) return;
        pendingClientUpdateReady = false;
        webView.evaluateJavascript(
                "window.dispatchEvent(new CustomEvent('aion-client-update-ready'));", null);
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

    /** 根据文件扩展名推断 MIME 类型 */
    private String guessMimeType(String path) {
        if (path.endsWith(".png"))  return "image/png";
        if (path.endsWith(".jpg") || path.endsWith(".jpeg")) return "image/jpeg";
        if (path.endsWith(".svg"))  return "image/svg+xml";
        if (path.endsWith(".ico"))  return "image/x-icon";
        if (path.endsWith(".webp")) return "image/webp";
        if (path.endsWith(".js"))   return "application/javascript";
        if (path.endsWith(".css"))  return "text/css";
        if (path.endsWith(".json")) return "application/json";
        if (path.endsWith(".mp3"))  return "audio/mpeg";
        if (path.endsWith(".mp4"))  return "video/mp4";
        return "application/octet-stream";
    }

    @Override
    protected void onDestroy() {
        mainHandler.removeCallbacksAndMessages(null);
        PhoneCameraPreviewCoordinator.shared().unregister(this);
        releasePhoneCameraPreview();
        if (phoneCameraBridge != null) {
            phoneCameraBridge.close();
            phoneCameraBridge = null;
        }
        cameraBridge = null;
        if (ringBleBridge != null) {
            ringBleBridge.close();
            ringBleBridge = null;
        }
        if (miBandBleBridge != null) {
            miBandBleBridge.destroy();
            miBandBleBridge = null;
        }
        if (mediaCacheStore != null) mediaCacheStore.shutdown();
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
