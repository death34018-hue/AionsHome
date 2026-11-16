package com.aion.chat;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.util.Base64;
import android.view.View;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

/** Displays model-authored HTML in a separate, offline WebView without app bridges. */
public final class ChatWebCardBridge {
    private final Activity activity;
    private final WebView chat;
    private final FrameLayout host;
    private FrameLayout viewport;
    private WebView player;
    private String activeKey;

    ChatWebCardBridge(Activity activity, WebView chat, FrameLayout host) {
        this.activity = activity;
        this.chat = chat;
        this.host = host;
    }

    @JavascriptInterface
    @SuppressLint("SetJavaScriptEnabled")
    public void open(String key, String html) {
        if (key == null || !key.matches("[a-z0-9-]{1,90}") || html == null || html.length() > 32000) return;
        activity.runOnUiThread(() -> {
            if (activity.isFinishing() || activity.isDestroyed()) return;
            destroy();
            activeKey = key;
            player = new WebView(activity);
            WebSettings settings = player.getSettings();
            settings.setJavaScriptEnabled(true);
            settings.setAllowFileAccess(false);
            settings.setAllowContentAccess(false);
            settings.setBlockNetworkLoads(true);
            player.setWebViewClient(new WebViewClient() {
                @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    return true;
                }
                @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                    String scheme = request.getUrl().getScheme();
                    if ("data".equals(scheme) || "blob".equals(scheme)) return null;
                    return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
                }
            });
            player.setWebChromeClient(new WebChromeClient() {
                @Override public boolean onConsoleMessage(ConsoleMessage message) {
                    String prefix = "web-card:" + key + ":";
                    if (key.equals(activeKey) && message.message().startsWith(prefix)) {
                        String detail = message.message().substring(prefix.length());
                        String args = JSONObject.quote(key) + "," + JSONObject.quote(detail.substring(0, Math.min(400, detail.length())));
                        chat.evaluateJavascript("(function notify(w){try{w.ChatWebCards?.nativeError(" + args +
                            ");for(let i=0;i<w.frames.length;i++)notify(w.frames[i])}catch(e){}})(window)", null);
                    }
                    return true;
                }
            });
            viewport = new FrameLayout(activity);
            viewport.setVisibility(View.INVISIBLE);
            viewport.addView(player, new FrameLayout.LayoutParams(1, 1));
            host.addView(viewport, new FrameLayout.LayoutParams(1, 1));
            String encoded = Base64.encodeToString(html.getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
            player.loadData(encoded, "text/html; charset=utf-8", "base64");
        });
    }

    @JavascriptInterface
    public void place(String key, int x, int y, int width, int height, int offsetX, int offsetY,
                      int pageWidth, int pageHeight, int viewportWidth) {
        activity.runOnUiThread(() -> {
            if (player == null || !activeKey.equals(key)) return;
            if (width <= 0 || height <= 0 || viewportWidth <= 0) {
                viewport.setVisibility(View.INVISIBLE);
                return;
            }
            float scale = (float) chat.getWidth() / viewportWidth;
            FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(
                    Math.min(host.getWidth(), Math.round(width * scale)),
                    Math.min(host.getHeight(), Math.round(height * scale)));
            params.leftMargin = Math.round(x * scale);
            params.topMargin = Math.round(y * scale);
            viewport.setLayoutParams(params);
            FrameLayout.LayoutParams content = new FrameLayout.LayoutParams(
                    Math.round(pageWidth * scale), Math.round(pageHeight * scale));
            player.setLayoutParams(content);
            player.setTranslationX(offsetX * scale);
            player.setTranslationY(offsetY * scale);
            viewport.setVisibility(View.VISIBLE);
        });
    }

    @JavascriptInterface
    public void close(String key) {
        activity.runOnUiThread(() -> { if (key != null && key.equals(activeKey)) destroy(); });
    }

    void destroy() {
        if (player == null) return;
        host.removeView(viewport);
        viewport.removeView(player);
        viewport = null;
        player.destroy();
        player = null;
        activeKey = null;
    }
}
