package com.aion.chat.widget;

import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** One-shot cache synchronization with no resident component or polling loop. */
public final class PrivateMemoSyncClient {
    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");
    private static final OkHttpClient CLIENT = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private PrivateMemoSyncClient() {}

    public static void sync(Context context, Runnable completion) {
        Context appContext = context.getApplicationContext();
        new Thread(() -> {
            try {
                syncBlocking(appContext);
            } catch (Exception ignored) {
                // Local pending rows remain available for the next foreground sync.
            }
            CompanionWidgetProvider.refreshAll(appContext);
            if (completion != null) completion.run();
        }, "private-memo-sync").start();
    }

    private static void syncBlocking(Context context) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences("aion_prefs", Context.MODE_PRIVATE);
        String savedPageUrl = prefs.getString("saved_url", "");
        HttpUrl pageUrl = savedPageUrl == null ? null : HttpUrl.parse(savedPageUrl);
        if (pageUrl == null) return;
        String cookie = CookieManager.getInstance().getCookie(savedPageUrl);
        try (PrivateMemoStore store = new PrivateMemoStore(context)) {
            for (PrivateMemo memo : store.pending()) {
                JSONObject body = new JSONObject();
                body.put("id", memo.serverId);
                body.put("content", memo.content);
                body.put("status", memo.status);
                body.put("source", memo.source);
                body.put("created_at", memo.createdAt / 1000.0);
                body.put("updated_at", memo.updatedAt / 1000.0);
                Request request = request(pageUrl, "/api/private-memos", cookie)
                        .post(RequestBody.create(body.toString(), JSON)).build();
                try (Response response = CLIENT.newCall(request).execute()) {
                    if (!response.isSuccessful()) return;
                }
                store.markSynced(memo.serverId);
            }
            List<PrivateMemo> serverMemos = new ArrayList<>();
            serverMemos.addAll(fetch(pageUrl, "active", cookie));
            serverMemos.addAll(fetch(pageUrl, "completed", cookie));
            store.replaceSynced(serverMemos);
        }
    }

    private static List<PrivateMemo> fetch(HttpUrl pageUrl, String status, String cookie)
            throws Exception {
        HttpUrl url = endpoint(pageUrl, "/api/private-memos").newBuilder()
                .addQueryParameter("status", status).build();
        Request.Builder builder = new Request.Builder().url(url).get();
        attachCookie(builder, cookie);
        try (Response response = CLIENT.newCall(builder.build()).execute()) {
            if (!response.isSuccessful() || response.body() == null) {
                throw new IllegalStateException("memo sync failed");
            }
            JSONArray rows = new JSONArray(response.body().string());
            List<PrivateMemo> result = new ArrayList<>();
            for (int i = 0; i < rows.length(); i++) {
                JSONObject row = rows.getJSONObject(i);
                result.add(new PrivateMemo(0L, row.optString("id"), row.optString("content"),
                        row.optString("status", "active"), row.optString("source", "app"),
                        (long) (row.optDouble("created_at", 0) * 1000.0),
                        (long) (row.optDouble("updated_at", 0) * 1000.0), "synced"));
            }
            return result;
        }
    }

    private static Request.Builder request(HttpUrl pageUrl, String path, String cookie) {
        Request.Builder builder = new Request.Builder().url(endpoint(pageUrl, path));
        attachCookie(builder, cookie);
        return builder;
    }

    private static HttpUrl endpoint(HttpUrl pageUrl, String path) {
        return pageUrl.newBuilder().encodedPath(path).query(null).fragment(null).build();
    }

    private static void attachCookie(Request.Builder builder, String cookie) {
        if (cookie != null && !cookie.trim().isEmpty()) builder.header("Cookie", cookie);
    }
}
