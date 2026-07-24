package com.aion.chat;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.aion.chat.miband.MiBandHistoryParser;
import com.aion.chat.miband.MiBandPreferences;
import com.aion.chat.miband.MiBandProtocol;
import com.aion.chat.miband.MiBandRuntime;
import com.aion.chat.miband.MiBandScanResult;
import com.aion.chat.miband.MiBandScanner;
import com.aion.chat.miband.MiBandStatus;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.List;

public final class AionMiBandBleBridge implements MiBandRuntime.Listener, MiBandScanner.Listener {
    private final WebView webView;
    private final Context context;
    private final Activity activity;
    private final MiBandRuntime runtime;
    private final MiBandScanner scanner;

    public AionMiBandBleBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context.getApplicationContext();
        this.activity = context instanceof Activity ? (Activity) context : null;
        runtime = MiBandRuntime.get(context);
        scanner = new MiBandScanner(context, this);
        runtime.addListener(this);
    }

    @JavascriptInterface public String getStatus() {
        return statusJson(runtime.status()).toString();
    }

    @JavascriptInterface public void saveConfig(String json) {
        try {
            JSONObject value = new JSONObject(json);
            MiBandPreferences.Settings settings = parseSettings(value);
            runtime.saveConfig(
                    value.optString("address", ""),
                    value.optString("name", "Xiaomi Smart Band 7"),
                    value.optString("auth_key", ""),
                    settings);
            notifyScheduleChanged();
            runtime.connectSaved();
        } catch (Exception error) {
            callError(error.getMessage());
        }
    }

    @JavascriptInterface public void connect() { runtime.connectSaved(); }
    @JavascriptInterface public void manualReconnect() { runtime.manualReconnect(); }
    @JavascriptInterface public void disconnect() { runtime.disconnect(); }
    @JavascriptInterface public void syncNow() { runtime.syncNow(); }

    @JavascriptInterface public void startScan() {
        if (!hasBluetoothPermissions()) {
            requestBluetoothPermissions();
            callError("请允许附近设备权限，然后再次点击扫描");
            return;
        }
        scanner.start();
    }

    @JavascriptInterface public void stopScan() { scanner.stop(); }

    @JavascriptInterface public void reselectDevice() {
        runtime.clearSavedDevice();
        startScan();
    }

    @JavascriptInterface public void selectDevice(String json) {
        try {
            JSONObject value = new JSONObject(json);
            MiBandPreferences.Settings settings = parseSettings(value);
            runtime.saveSelectedDevice(
                    value.optString("address", ""),
                    value.optString("name", "Xiaomi Smart Band 7"),
                    value.optString("auth_key", ""),
                    settings);
            scanner.stop();
            notifyScheduleChanged();
            runtime.connectSaved();
        } catch (Exception error) {
            callError(error.getMessage());
        }
    }

    @JavascriptInterface public void setSchedule(String json) {
        try {
            runtime.updateSchedule(parseSettings(new JSONObject(json)));
            notifyScheduleChanged();
        } catch (Exception error) {
            callError(error.getMessage());
        }
    }

    @JavascriptInterface public void startRealtime() { runtime.startRealtime(); }
    @JavascriptInterface public void stopRealtime() { runtime.stopRealtime(); }
    @JavascriptInterface public void vibrate(String pattern) { runtime.vibrate(pattern); }

    public void destroy() {
        scanner.stop();
        runtime.removeListener(this);
    }

    @Override public void onStatus(MiBandStatus status) {
        callJs("onStatus", statusJson(status));
    }

    @Override public void onSamples(List<MiBandProtocol.ActivitySample> samples) {
        JSONArray items = new JSONArray();
        for (MiBandProtocol.ActivitySample sample : samples) {
            JSONObject item = new JSONObject();
            try {
                item.put("heart_rate", sample.heartRate);
                item.put("measured_at", sample.timestampMillis / 1000.0);
                item.put("steps", sample.steps);
                item.put("intensity", sample.intensity);
                item.put("raw_kind", sample.rawKind);
                item.put("unknown", sample.unknown);
                item.put("sleep", sample.sleep);
                item.put("deep_sleep", sample.deepSleep);
                item.put("rem_sleep", sample.remSleep);
                if (sample.sleepStage != null) item.put("sleep_stage", sample.sleepStage);
                items.put(item);
            } catch (Exception ignored) {}
        }
        JSONObject payload = new JSONObject();
        try { payload.put("items", items); } catch (Exception ignored) {}
        callJs("onSamples", payload);
    }

    @Override public void onScanState(boolean scanning, String message) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("scanning", scanning);
            payload.put("message", message == null ? "" : message);
        } catch (Exception ignored) {}
        callJs("onScanState", payload);
    }

    @Override public void onScanResults(List<MiBandScanResult> results) {
        JSONArray items = new JSONArray();
        for (MiBandScanResult result : results) {
            JSONObject item = new JSONObject();
            try {
                item.put("address", result.address);
                item.put("name", result.name);
                item.put("rssi", result.rssi);
                items.put(item);
            } catch (Exception ignored) {}
        }
        JSONObject payload = new JSONObject();
        try { payload.put("items", items); } catch (Exception ignored) {}
        callJs("onScanResults", payload);
    }

    private MiBandPreferences.Settings parseSettings(JSONObject value) {
        MiBandPreferences.Settings current = runtime.settings();
        return new MiBandPreferences.Settings(
                value.optInt("day_interval_minutes", current.dayIntervalMinutes),
                value.has("night_start")
                        ? MiBandPreferences.parseClock(value.optString("night_start"))
                        : current.nightStartMinute,
                value.has("night_end")
                        ? MiBandPreferences.parseClock(value.optString("night_end"))
                        : current.nightEndMinute,
                value.optInt("night_interval_minutes", current.nightIntervalMinutes));
    }

    private JSONObject statusJson(MiBandStatus status) {
        JSONObject out = new JSONObject();
        MiBandPreferences.Settings settings = runtime.settings();
        try {
            out.put("state", status.state);
            out.put("device_name", status.deviceName);
            out.put("configured", status.configured);
            out.put("connected", status.connected);
            out.put("authenticated", status.authenticated);
            out.put("realtime", status.realtime);
            out.put("syncing", status.syncing);
            out.put("battery", status.battery);
            out.put("latest_heart_rate", status.latestHeartRate);
            out.put("latest_heart_rate_at", status.latestHeartRateAt / 1000.0);
            out.put("last_sync_at", status.lastSyncAt / 1000.0);
            out.put("next_sync_at", status.nextSyncAt / 1000.0);
            out.put("error", status.error);
            out.put("saved_device_name", runtime.savedDeviceName());
            out.put("saved_device_address", runtime.savedDeviceMaskedAddress());
            out.put("auth_key_configured", runtime.authKeyConfigured());
            out.put("day_interval_minutes", settings.dayIntervalMinutes);
            out.put("night_start", MiBandPreferences.formatClock(settings.nightStartMinute));
            out.put("night_end", MiBandPreferences.formatClock(settings.nightEndMinute));
            out.put("night_interval_minutes", settings.nightIntervalMinutes);
        } catch (Exception ignored) {}
        return out;
    }

    private void callError(String message) {
        JSONObject payload = new JSONObject();
        try { payload.put("message", message == null ? "操作失败" : message); } catch (Exception ignored) {}
        callJs("onError", payload);
    }

    private void notifyScheduleChanged() {
        Intent intent = new Intent(context, AionPushService.class);
        intent.putExtra("action", AionPushService.ACTION_MI_BAND_SETTINGS_CHANGED);
        context.startService(intent);
    }

    private boolean hasBluetoothPermissions() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true;
        return ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_SCAN)
                == PackageManager.PERMISSION_GRANTED
                && ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_CONNECT)
                == PackageManager.PERMISSION_GRANTED;
    }

    private void requestBluetoothPermissions() {
        if (activity == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return;
        ActivityCompat.requestPermissions(activity, new String[]{
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.BLUETOOTH_CONNECT
        }, 5002);
    }

    private void callJs(String method, JSONObject payload) {
        String script = "(function(){var p=" + payload.toString()
                + ",m=" + JSONObject.quote(method)
                + ";function send(w){try{var h=w.miBandNative;if(h&&typeof h[m]==='function')h[m](p);}catch(e){}}"
                + "send(window);var f=document.querySelectorAll('iframe');"
                + "for(var i=0;i<f.length;i++)send(f[i].contentWindow);})();";
        webView.post(() -> webView.evaluateJavascript(script, null));
    }
}
