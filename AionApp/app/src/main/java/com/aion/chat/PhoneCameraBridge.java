package com.aion.chat;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.provider.Settings;
import android.util.Base64;
import android.webkit.JavascriptInterface;

import androidx.core.content.ContextCompat;

import java.util.concurrent.atomic.AtomicInteger;

public final class PhoneCameraBridge {
    private final Activity activity;
    private final PhoneCameraController previewController;
    private volatile String previewFrame = "";
    private volatile String previewError = "";
    private final AtomicInteger previewGeneration = new AtomicInteger();
    private volatile boolean previewVisible;
    private volatile boolean appForeground;

    PhoneCameraBridge(Activity activity) {
        this.activity = activity;
        this.previewController = new PhoneCameraController(activity);
    }

    @JavascriptInterface
    public String getClientId() {
        String value = Settings.Secure.getString(
                activity.getContentResolver(), Settings.Secure.ANDROID_ID);
        return "android-push:" + (value == null ? "unknown-device" : value);
    }

    @JavascriptInterface
    public String getCapabilities() {
        return previewController.getCapabilities().toString();
    }

    @JavascriptInterface
    public boolean hasPermission() {
        return ContextCompat.checkSelfPermission(activity, Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED;
    }

    @JavascriptInterface
    public synchronized boolean requestPreview(String facing, float zoom) {
        if (!hasPermission()) {
            previewError = "camera_permission_denied";
            return false;
        }
        if (PhoneCameraPreviewCoordinator.shared().isEventActive()) {
            previewError = "event_capture_active";
            return false;
        }
        if (!appForeground) {
            previewError = "app_background";
            return false;
        }
        if (!previewVisible) {
            previewError = "preview_not_visible";
            return false;
        }
        final int generation = previewGeneration.incrementAndGet();
        boolean accepted = previewController.capture(
                facing, zoom, new PhoneCameraController.CaptureCallback() {
            @Override
            public void onSuccess(byte[] jpeg, org.json.JSONObject metadata) {
                if (generation != previewGeneration.get()) return;
                previewFrame = Base64.encodeToString(jpeg, Base64.NO_WRAP);
                previewError = "";
            }

            @Override
            public void onFailure(String error) {
                if (generation != previewGeneration.get()) return;
                previewError = error;
            }
        });
        return accepted;
    }

    @JavascriptInterface
    public String getPreviewFrame() {
        return previewFrame;
    }

    @JavascriptInterface
    public String getPreviewError() {
        return previewError;
    }

    @JavascriptInterface
    public synchronized void stopPreview() {
        pausePreview();
    }

    @JavascriptInterface
    public synchronized void setPreviewVisible(boolean visible) {
        previewVisible = visible;
        if (!visible) pausePreview();
    }

    @JavascriptInterface
    public boolean arm(String facing, float zoom) {
        if (!hasPermission()) return false;
        Intent intent = new Intent(activity, AionPushService.class);
        intent.putExtra("action", AionPushService.ACTION_ARM_PHONE_CAMERA);
        intent.putExtra(AionPushService.EXTRA_PHONE_CAMERA_FACING, facing);
        intent.putExtra(AionPushService.EXTRA_PHONE_CAMERA_ZOOM, zoom);
        ContextCompat.startForegroundService(activity, intent);
        return true;
    }

    @JavascriptInterface
    public void disarm() {
        Intent intent = new Intent(activity, AionPushService.class);
        intent.putExtra("action", AionPushService.ACTION_DISARM_PHONE_CAMERA);
        ContextCompat.startForegroundService(activity, intent);
    }

    synchronized void close() {
        pausePreview();
    }

    synchronized void pausePreview() {
        previewGeneration.incrementAndGet();
        previewController.close();
        previewFrame = "";
        previewError = "";
    }

    boolean isPreviewVisible() {
        return previewVisible;
    }

    synchronized void setAppForeground(boolean foreground) {
        appForeground = foreground;
        if (!foreground) pausePreview();
    }
}
