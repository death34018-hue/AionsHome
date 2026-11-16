package com.aion.chat;

import android.app.Activity;
import android.graphics.Color;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/** Plays the bundled welcome page above the activity that is already starting. */
final class LaunchLoginOverlay {
    private WebView page;

    void show(Activity activity) {
        WebView view = new WebView(activity);
        page = view;
        view.setBackgroundColor(Color.BLACK);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setBlockNetworkLoads(true);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccess(false); // android_asset remains available.
        view.addJavascriptInterface(new Object() {
            @JavascriptInterface public void enterHome(int fadeDurationMs) {
                view.post(() -> {
                    if (page != view) return;
                    view.animate().alpha(0f).setDuration(fadeDurationMs)
                            .withEndAction(() -> dismiss()).start();
                });
            }
        }, "AionLaunchLogin");
        view.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) view.post(() -> dismiss());
            }
        });
        activity.addContentView(view, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        view.requestFocus();
        view.loadUrl("file:///android_asset/launch-login/index-video.html");
    }

    boolean dismiss() {
        if (page == null) return false;
        WebView view = page;
        page = null;
        view.animate().cancel();
        ((ViewGroup) view.getParent()).removeView(view);
        view.destroy();
        return true;
    }
}
