package com.aion.chat;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;

/** Reads vivo SuperX cards only while discovery or an active card requires it. */
public final class SuperXNotificationMonitor {
    public interface Sink {
        void onPosted(JSONObject data);
        void onRemoved(String key, double observedAt);
    }

    private static final String TAG = "AionSuperX";
    private static final long DISCOVERY_THROTTLE_MS = 30_000L;
    private static final long ACTIVE_REFRESH_MS = 75_000L;
    private static final int MAX_DUMP_CHARS = 2_000_000;

    private final Context context;
    private final Executor executor;
    private final Sink sink;
    private final Map<String, String> fingerprints = new HashMap<>();
    private boolean active;
    private boolean scanning;
    private boolean closed;
    private long lastScanAtMs;

    public SuperXNotificationMonitor(Context context, Executor executor, Sink sink) {
        this.context = context.getApplicationContext();
        this.executor = executor;
        this.sink = sink;
    }

    public void discoverNow() {
        requestScan(true);
    }

    public void discoverForForegroundApp(String packageName) {
        if (isLikelyLiveCardApp(packageName)) requestScan(true);
    }

    public void refreshActive() {
        requestScan(false);
    }

    public synchronized void close() {
        closed = true;
    }

    private synchronized void requestScan(boolean discovery) {
        if (closed || scanning || (!discovery && !active)) return;
        long now = System.currentTimeMillis();
        long interval = active ? ACTIVE_REFRESH_MS : DISCOVERY_THROTTLE_MS;
        if (now - lastScanAtMs < interval) return;
        scanning = true;
        lastScanAtMs = now;
        try {
            executor.execute(this::scanAndPublish);
        } catch (RuntimeException error) {
            scanning = false;
            Log.d(TAG, "scan scheduling skipped: " + error.getMessage());
        }
    }

    private void scanAndPublish() {
        try {
            if (context.checkSelfPermission(Manifest.permission.DUMP)
                    != PackageManager.PERMISSION_GRANTED) {
                Log.d(TAG, "DUMP permission not granted");
                return;
            }
            List<SuperXDumpParser.Card> cards = SuperXDumpParser.parse(readNotificationDump());
            reconcile(cards, System.currentTimeMillis());
        } catch (Exception error) {
            Log.d(TAG, "scan skipped: " + error.getMessage());
        } finally {
            synchronized (this) {
                scanning = false;
            }
        }
    }

    private void reconcile(List<SuperXDumpParser.Card> cards, long nowMs) throws Exception {
        Set<String> seen = new HashSet<>();
        boolean hasNonTerminal = false;
        for (SuperXDumpParser.Card card : cards) {
            if (context.getPackageName().equals(card.packageName)) continue;
            String identity = NotificationContextPolicy.liveIdentity(card.packageName, card.scene);
            seen.add(identity);
            String fingerprint = NotificationContextPolicy.fingerprint(
                    card.title, card.text, card.scene, false);
            String previous = fingerprints.put(identity, fingerprint);
            if (!fingerprint.equals(previous)) sink.onPosted(payload(card, identity, nowMs));
            if (!card.terminal) hasNonTerminal = true;
        }

        for (String identity : new HashSet<>(fingerprints.keySet())) {
            if (seen.contains(identity)) continue;
            fingerprints.remove(identity);
            sink.onRemoved(identity, nowMs / 1000.0);
        }
        synchronized (this) {
            active = hasNonTerminal;
        }
        Log.d(TAG, "scan cards=" + cards.size() + " active=" + hasNonTerminal);
    }

    private JSONObject payload(SuperXDumpParser.Card card, String identity, long nowMs)
            throws Exception {
        JSONObject data = new JSONObject();
        data.put("key", identity);
        data.put("package_name", card.packageName);
        data.put("app_name", appName(card.packageName));
        data.put("posted_at", nowMs / 1000.0);
        data.put("observed_at", nowMs / 1000.0);
        data.put("title", card.title);
        data.put("text", card.text);
        data.put("category", "status");
        data.put("channel_id", "vivo_superx");
        data.put("ongoing", !card.terminal);
        data.put("group_summary", false);
        data.put("noise", false);
        return data;
    }

    private String readNotificationDump() throws Exception {
        Process process = new ProcessBuilder(
                "/system/bin/dumpsys", "notification", "--noredact")
                .redirectErrorStream(true)
                .start();
        StringBuilder output = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                process.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null && output.length() < MAX_DUMP_CHARS) {
                output.append(line).append('\n');
            }
        }
        if (!process.waitFor(5, TimeUnit.SECONDS)) {
            process.destroy();
            throw new IllegalStateException("notification dump timed out");
        }
        if (process.exitValue() != 0) {
            throw new IllegalStateException("notification dump exit=" + process.exitValue());
        }
        return output.toString();
    }

    private String appName(String packageName) {
        try {
            return context.getPackageManager().getApplicationLabel(
                    context.getPackageManager().getApplicationInfo(packageName, 0)).toString();
        } catch (Exception ignored) {
            return packageName;
        }
    }

    private static boolean isLikelyLiveCardApp(String packageName) {
        if (packageName == null) return false;
        String value = packageName.trim();
        return "me.ele".equals(value)
                || "com.sankuai.meituan.takeoutnew".equals(value)
                || "com.taobao.taobao".equals(value)
                || "com.wudaokou.hippo".equals(value);
    }
}
