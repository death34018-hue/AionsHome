package com.aion.chat;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.net.Uri;
import android.os.Environment;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.widget.Toast;

/** Download authenticated attachments and novel exports without exposing cookies to JavaScript. */
public final class RepairFileBridge {
    private final Activity activity;
    private final String homeUrl;

    public RepairFileBridge(Activity activity, String homeUrl) {
        this.activity = activity;
        this.homeUrl = homeUrl;
    }

    @JavascriptInterface
    public boolean supportsTheaterExport() { return true; }

    @JavascriptInterface
    public void save(String url, String filename) {
        activity.runOnUiThread(() -> {
            try {
                Uri uri = Uri.parse(url);
                if (!("http".equals(uri.getScheme()) || "https".equals(uri.getScheme()))
                        || !ConnectionEndpoint.isAllowedContentHost(uri.getHost(), homeUrl)
                        || uri.getUserInfo() != null
                        || uri.getPath() == null
                        || !(uri.getPath().matches("/api/repair/files/[a-f0-9]{32}")
                             || uri.getPath().matches("/api/theater/studio/books/[A-Za-z0-9_-]+/export"))) {
                    throw new IllegalArgumentException("不是小家的附件或小说导出地址");
                }
                String name = filename == null ? "attachment" : filename.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
                if (name.trim().isEmpty() || ".".equals(name) || "..".equals(name)) name = "attachment";
                DownloadManager.Request request = new DownloadManager.Request(uri);
                String cookie = CookieManager.getInstance().getCookie(url);
                if (cookie != null && !cookie.isEmpty()) request.addRequestHeader("Cookie", cookie);
                request.setTitle(name);
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                DownloadManager downloads = (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
                downloads.enqueue(request);
                Toast.makeText(activity, "已开始下载，可在下载通知中查看结果", Toast.LENGTH_LONG).show();
            } catch (Exception error) {
                Toast.makeText(activity, "下载未开始：" + error.getMessage(), Toast.LENGTH_LONG).show();
            }
        });
    }
}
