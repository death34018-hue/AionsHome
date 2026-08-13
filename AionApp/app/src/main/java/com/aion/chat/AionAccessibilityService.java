package com.aion.chat;

import android.accessibilityservice.AccessibilityService;
import android.content.ComponentName;
import android.graphics.Bitmap;
import android.graphics.ColorSpace;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.Base64;
import android.util.Log;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.concurrent.TimeUnit;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public class AionAccessibilityService extends AccessibilityService {
    private static final String TAG = "AionAccessibility";
    private static final long MIN_CAPTURE_INTERVAL_MS = 8_000;
    private static final long FORCE_CAPTURE_INTERVAL_MS = 2_500;
    private static final long FOREGROUND_RECONCILE_DELAY_MS = 400L;
    private static volatile AionAccessibilityService instance;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .cookieJar(new WebViewCookieJar())
            .build();

    private volatile long lastCaptureStartedAt = 0;
    private volatile String lastPackageName = "";
    private volatile boolean serviceActive = false;
    private volatile String serverHttpBase = "";

    public static boolean isReady() {
        return instance != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R;
    }

    public static boolean captureLatest(Context context, String app, String reason, boolean force, long delayMs, String httpBase) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return false;
        }
        AionAccessibilityService svc = instance;
        if (svc == null) {
            return false;
        }
        if (httpBase != null && !httpBase.isEmpty()) {
            svc.serverHttpBase = httpBase;
        }
        svc.queueCapture(app, reason, force, delayMs);
        return true;
    }

    public static boolean isEnabledInSettings(Context context) {
        try {
            String enabled = Settings.Secure.getString(
                    context.getContentResolver(),
                    Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            );
            if (enabled == null || enabled.isEmpty()) return false;
            ComponentName expected = new ComponentName(context, AionAccessibilityService.class);
            TextUtils.SimpleStringSplitter splitter = new TextUtils.SimpleStringSplitter(':');
            splitter.setString(enabled);
            while (splitter.hasNext()) {
                String service = splitter.next();
                ComponentName component = ComponentName.unflattenFromString(service);
                if (component != null && expected.equals(component)) return true;
                if (expected.flattenToShortString().equalsIgnoreCase(service)) return true;
                if (expected.flattenToString().equalsIgnoreCase(service)) return true;
            }
        } catch (Exception e) {
            Log.d(TAG, "accessibility settings check failed: " + e.getMessage());
        }
        return false;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        serviceActive = true;
        instance = this;
        // 标记用户曾主动开启过无障碍，用于自动恢复判断
        getSharedPreferences("aion_prefs", MODE_PRIVATE)
                .edit().putBoolean("accessibility_user_opted_in", true).apply();
        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.start(this);
        runtime.onAccessibilityConnected();
        Log.i(TAG, "Accessibility screenshot service connected");
    }

    @Override
    public void onDestroy() {
        Log.i(TAG, "Accessibility screenshot service destroyed");
        serviceActive = false;
        mainHandler.removeCallbacksAndMessages(null);
        if (instance == this) instance = null;
        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.get();
        if (runtime != null) runtime.onAccessibilityUnavailable();
        super.onDestroy();
    }

    @Override
    public boolean onUnbind(Intent intent) {
        Log.i(TAG, "Accessibility screenshot service unbound");
        serviceActive = false;
        mainHandler.removeCallbacksAndMessages(null);
        if (instance == this) instance = null;
        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.get();
        if (runtime != null) runtime.onAccessibilityUnavailable();
        return super.onUnbind(intent);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null || event.getPackageName() == null) return;
        int type = event.getEventType();
        if (type != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                && type != AccessibilityEvent.TYPE_WINDOWS_CHANGED) {
            return;
        }

        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.get();
        if (runtime == null) return;
        mainHandler.removeCallbacks(foregroundReconcile);
        mainHandler.postDelayed(foregroundReconcile, FOREGROUND_RECONCILE_DELAY_MS);
    }

    @Override
    public void onInterrupt() {
        Log.i(TAG, "Accessibility service interrupted");
    }

    private final Runnable foregroundReconcile = () -> {
        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.get();
        if (runtime != null) runtime.reconcileForegroundOnce();
    };

    private void queueCapture(String app, String reason, boolean force, long delayMs) {
        if (!serviceActive) return;
        long now = System.currentTimeMillis();
        long minInterval = force ? FORCE_CAPTURE_INTERVAL_MS : MIN_CAPTURE_INTERVAL_MS;
        if (now - lastCaptureStartedAt < minInterval) {
            Log.d(TAG, "screenshot throttled reason=" + reason);
            return;
        }
        lastCaptureStartedAt = now;
        String targetApp = (app == null || app.isEmpty()) ? lastPackageName : app;
        long safeDelay = Math.max(0, delayMs);
        mainHandler.postDelayed(() -> {
            if (serviceActive) takeAndUploadScreenshot(targetApp, reason);
        }, safeDelay);
    }

    private boolean isPhoneUnlockedForCapture() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT_WATCH && !pm.isInteractive()) {
                return false;
            }
            KeyguardManager kg = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
            if (kg != null) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && kg.isDeviceLocked()) return false;
                if (kg.isKeyguardLocked()) return false;
            }
        } catch (Exception e) {
            Log.w(TAG, "lock state check failed: " + e.getMessage());
            return false;
        }
        return true;
    }

    private void takeAndUploadScreenshot(String app, String reason) {
        if (!serviceActive) return;
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            postSkip("accessibility_api_unavailable", app, false);
            return;
        }
        if (!isPhoneUnlockedForCapture()) {
            postSkip("locked", app, true);
            return;
        }

        takeScreenshot(Display.DEFAULT_DISPLAY, getMainExecutor(), new TakeScreenshotCallback() {
            @Override
            public void onSuccess(ScreenshotResult screenshot) {
                if (!serviceActive) return;
                Bitmap wrapped = null;
                Bitmap copy = null;
                Bitmap scaled = null;
                HardwareBuffer buffer = null;
                try {
                    buffer = screenshot.getHardwareBuffer();
                    ColorSpace colorSpace = screenshot.getColorSpace();
                    wrapped = Bitmap.wrapHardwareBuffer(buffer, colorSpace);
                    if (wrapped == null) {
                        postSkip("accessibility_empty_bitmap", app, false);
                        return;
                    }
                    copy = wrapped.copy(Bitmap.Config.ARGB_8888, false);
                    int width = copy.getWidth();
                    int height = copy.getHeight();
                    float scale = Math.min(1f, 1080f / Math.max(width, height));
                    if (scale < 1f) {
                        int sw = Math.max(1, Math.round(width * scale));
                        int sh = Math.max(1, Math.round(height * scale));
                        scaled = Bitmap.createScaledBitmap(copy, sw, sh, true);
                    } else {
                        scaled = copy;
                    }

                    ByteArrayOutputStream out = new ByteArrayOutputStream();
                    scaled.compress(Bitmap.CompressFormat.JPEG, 82, out);
                    String b64 = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
                    uploadBase64(b64, app, reason);
                } catch (Exception e) {
                    Log.e(TAG, "accessibility screenshot failed: " + e.getMessage());
                    postSkip("accessibility_capture_failed", app, false);
                } finally {
                    if (buffer != null) {
                        try { buffer.close(); } catch (Exception ignored) {}
                    }
                    if (copy != null && copy != scaled) copy.recycle();
                    if (scaled != null) scaled.recycle();
                }
            }

            @Override
            public void onFailure(int errorCode) {
                if (!serviceActive) return;
                postSkip("accessibility_error_" + errorCode, app, false);
            }
        });
    }

    private String getHttpBase() {
        if (serverHttpBase != null && !serverHttpBase.isEmpty()) {
            return serverHttpBase;
        }
        SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
        String saved = prefs.getString("saved_url", "http://192.168.1.100:8080/chat");
        return saved.replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "")
                .replace("/chat", "");
    }

    private void uploadBase64(String b64, String app, String reason) {
        new Thread(() -> uploadBase64OnBackground(b64, app, reason), "AionAccessibilityUpload").start();
    }

    private void uploadBase64OnBackground(String b64, String app, String reason) {
        String url = getHttpBase() + "/api/phone-screen/upload";
        try {
            JSONObject body = new JSONObject();
            body.put("image_base64", b64);
            body.put("timestamp", System.currentTimeMillis() / 1000.0);
            body.put("app", app == null ? "" : app);
            body.put("locked", false);
            body.put("reason", reason == null ? "" : reason);
            body.put("source", "accessibility");

            MediaType jsonType = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), jsonType);
            Request req = new Request.Builder()
                    .url(url)
                    .post(reqBody)
                    .build();
            try (Response resp = client.newCall(req).execute()) {
                Log.i(TAG, "accessibility phone screen uploaded -> " + resp.code() + " " + url);
            }
        } catch (Exception e) {
            Log.e(TAG, "accessibility upload failed: " + e.getClass().getSimpleName() + ":" + e.getMessage() + " url=" + url);
        }
    }

    private void postSkip(String reason, String app, boolean locked) {
        new Thread(() -> postSkipOnBackground(reason, app, locked), "AionAccessibilitySkip").start();
    }

    private void postSkipOnBackground(String reason, String app, boolean locked) {
        String url = getHttpBase() + "/api/phone-screen/skip";
        try {
            JSONObject body = new JSONObject();
            body.put("reason", reason);
            body.put("app", app == null ? "" : app);
            body.put("locked", locked);
            MediaType jsonType = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), jsonType);
            Request req = new Request.Builder()
                    .url(url)
                    .post(reqBody)
                    .build();
            try (Response resp = client.newCall(req).execute()) {
                Log.d(TAG, "accessibility phone screen skipped " + reason + " -> " + resp.code());
            }
        } catch (Exception e) {
            Log.d(TAG, "accessibility skip report failed: " + e.getClass().getSimpleName() + ":" + e.getMessage() + " url=" + url);
        }
    }
}
