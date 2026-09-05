package com.aion.chat.widget;

import android.app.Activity;
import android.content.Intent;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

public final class PrivateMemoBridge {
    private final Activity activity;
    private final WebView webView;

    public PrivateMemoBridge(Activity activity, WebView webView) {
        this.activity = activity;
        this.webView = webView;
    }

    @JavascriptInterface
    public void openRecorder() {
        activity.runOnUiThread(() -> {
            Intent intent = new Intent(activity, WidgetRecordActivity.class);
            intent.putExtra("memo_source", "app");
            activity.startActivity(intent);
        });
    }

    @JavascriptInterface
    public void refreshWidget() {
        sync();
    }

    public void sync() {
        PrivateMemoSyncClient.sync(activity, () -> activity.runOnUiThread(() ->
                webView.evaluateJavascript("window.onAionPrivateMemosChanged?.()", null)));
    }
}
