package com.aion.chat.widget;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** One-shot widget synchronization, triggered only by updates and user actions. */
public final class WidgetStateSyncClient {
    private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");
    private static final String[] ACTORS = {"aion", "connor"};
    private static final AtomicBoolean RUNNING = new AtomicBoolean(false);
    private static final OkHttpClient CLIENT = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build();

    private WidgetStateSyncClient() {}

    public static void sync(Context context) {
        Context appContext = context.getApplicationContext();
        if (!RUNNING.compareAndSet(false, true)) return;
        new Thread(() -> {
            try {
                syncBlocking(appContext);
            } catch (Exception error) {
                // Pending actions remain queued for the next foreground/push refresh.
                Log.w("WidgetStateSync", "widget sync failed", error);
            } finally {
                RUNNING.set(false);
                CompanionWidgetProvider.refreshAll(appContext);
            }
        }, "widget-state-sync").start();
    }

    private static void syncBlocking(Context context) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences("aion_prefs", Context.MODE_PRIVATE);
        String savedPageUrl = prefs.getString("saved_url", "");
        HttpUrl pageUrl = savedPageUrl == null ? null : HttpUrl.parse(savedPageUrl);
        if (pageUrl == null) return;
        String cookie = readCookieOnMainThread(savedPageUrl);
        WidgetStateStore state = new WidgetStateStore(context);

        for (String actorId : ACTORS) {
            String pending = state.pendingActorState(actorId);
            if (pending.isEmpty()) continue;
            JSONObject body = new JSONObject().put("state", pending);
            Request request = request(pageUrl, "/api/widget-control/actors/" + actorId, cookie)
                    .patch(RequestBody.create(body.toString(), JSON)).build();
            try (Response response = CLIENT.newCall(request).execute()) {
                if (!response.isSuccessful()) return;
                state.clearPendingActorState(actorId, pending);
            }
        }
        if (state.pendingBannerClear()) {
            Request request = request(pageUrl, "/api/widget-control/banner/clear", cookie)
                    .post(RequestBody.create("{}", JSON)).build();
            try (Response response = CLIENT.newCall(request).execute()) {
                if (!response.isSuccessful()) return;
                state.clearPendingBannerClear();
            }
        }

        Request request = request(pageUrl, "/api/widget-control/state", cookie).get().build();
        JSONObject payload;
        try (Response response = CLIENT.newCall(request).execute()) {
            if (!response.isSuccessful() || response.body() == null) return;
            payload = new JSONObject(response.body().string());
        }
        JSONObject actors = payload.optJSONObject("actors");
        if (actors != null) {
            for (String actorId : ACTORS) {
                JSONObject actor = actors.optJSONObject(actorId);
                if (actor == null) continue;
                String current = actor.optString("current_state", "");
                JSONArray states = actor.optJSONArray("states");
                JSONObject asset = actor.optJSONObject("asset");
                String path = "";
                String version = asset == null ? "" : asset.optString("version", "");
                if (!WidgetAssetSyncDecision.needsDownload(
                        state.assetVersion(actorId), state.assetPath(actorId), version)) {
                    path = state.assetPath(actorId);
                } else if (asset != null) {
                    path = downloadAsset(context, pageUrl, asset.optString("url", ""),
                            actorId, 240, 360, cookie);
                }
                state.applyActor(actorId, current, states, path, version);
            }
        }
        JSONObject bannerAsset = payload.optJSONObject("banner_asset");
        if (bannerAsset != null) {
            String version = bannerAsset.optString("version", "");
            if (WidgetAssetSyncDecision.needsDownload(
                    state.bannerAssetVersion(), state.bannerAssetPath(), version)) {
                String path = downloadAsset(context, pageUrl,
                        bannerAsset.optString("url", ""), "banner", 480, 300, cookie);
                state.applyBannerAsset(path, version);
            }
        }
        JSONObject banner = payload.optJSONObject("banner");
        String bannerText = banner == null ? "" : banner.optString("content", "").trim();
        if (bannerText.isEmpty()) WidgetBannerState.clear(context);
        else WidgetBannerState.show(context, bannerText);
    }

    private static String downloadAsset(Context context, HttpUrl pageUrl, String relativeUrl,
                                        String cacheName, int maxWidth, int maxHeight,
                                        String cookie) throws Exception {
        HttpUrl url = pageUrl.resolve(relativeUrl);
        if (url == null) return "";
        Request.Builder builder = new Request.Builder().url(url).get();
        attachCookie(builder, cookie);
        byte[] bytes;
        try (Response response = CLIENT.newCall(builder.build()).execute()) {
            if (!response.isSuccessful() || response.body() == null) return "";
            bytes = response.body().bytes();
        }
        Bitmap original = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
        if (original == null) return "";
        float scale = Math.min(1f, Math.min((float) maxWidth / original.getWidth(),
                (float) maxHeight / original.getHeight()));
        Bitmap bitmap = original;
        if (scale < 1f) {
            bitmap = Bitmap.createScaledBitmap(original,
                    Math.max(1, Math.round(original.getWidth() * scale)),
                    Math.max(1, Math.round(original.getHeight() * scale)), true);
        }
        File directory = new File(context.getFilesDir(), "widget_assets");
        if (!directory.exists() && !directory.mkdirs()) return "";
        File target = new File(directory, cacheName + ".png");
        try (FileOutputStream output = new FileOutputStream(target)) {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, output);
        } finally {
            if (bitmap != original) bitmap.recycle();
            original.recycle();
        }
        return target.getAbsolutePath();
    }

    private static Request.Builder request(HttpUrl pageUrl, String path, String cookie) {
        Request.Builder builder = new Request.Builder()
                .url(pageUrl.newBuilder().encodedPath(path).query(null).fragment(null).build());
        attachCookie(builder, cookie);
        return builder;
    }

    private static void attachCookie(Request.Builder builder, String cookie) {
        if (cookie != null && !cookie.trim().isEmpty()) builder.header("Cookie", cookie);
    }

    private static String readCookieOnMainThread(String pageUrl) throws InterruptedException {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            return CookieManager.getInstance().getCookie(pageUrl);
        }
        AtomicReference<String> result = new AtomicReference<>("");
        AtomicReference<RuntimeException> failure = new AtomicReference<>();
        CountDownLatch ready = new CountDownLatch(1);
        new Handler(Looper.getMainLooper()).post(() -> {
            try {
                result.set(CookieManager.getInstance().getCookie(pageUrl));
            } catch (RuntimeException error) {
                failure.set(error);
            } finally {
                ready.countDown();
            }
        });
        if (!ready.await(5, TimeUnit.SECONDS)) {
            throw new IllegalStateException("cookie initialization timed out");
        }
        if (failure.get() != null) throw failure.get();
        return result.get();
    }
}
