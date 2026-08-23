package com.aion.chat;

import android.app.Notification;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.text.TextUtils;
import android.util.Log;

import androidx.core.content.ContextCompat;

import org.json.JSONObject;

import java.util.concurrent.ConcurrentHashMap;

/** Extracts text-only notification context and forwards it to the existing push service. */
public final class AionNotificationListenerService extends NotificationListenerService {
    private static final String TAG = "AionNotifContext";
    private static final String SUPERX_PREFIX = "notification.superx.";
    private final ConcurrentHashMap<String, String> forwardedPayloads = new ConcurrentHashMap<>();

    @Override public void onListenerConnected() {
        super.onListenerConnected();
        try {
            StatusBarNotification[] active = getActiveNotifications();
            if (active == null) return;
            for (StatusBarNotification sbn : active) {
                processNotification(sbn);
            }
        } catch (RuntimeException error) {
            Log.d(TAG, "active notification backfill skipped: " + error.getMessage());
        }
    }

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        processNotification(sbn);
    }

    private void processNotification(StatusBarNotification sbn) {
        if (sbn == null || sbn.getNotification() == null) return;
        try {
            long nowMs = System.currentTimeMillis();
            if (!NotificationContextPolicy.isFresh(sbn.getPostTime(), nowMs)) return;
            Notification notification = sbn.getNotification();
            CharSequence titleValue = notification.extras.getCharSequence(Notification.EXTRA_TITLE);
            CharSequence textValue = notification.extras.getCharSequence(Notification.EXTRA_BIG_TEXT);
            if (TextUtils.isEmpty(textValue)) {
                textValue = notification.extras.getCharSequence(Notification.EXTRA_TEXT);
            }
            boolean liveCard = isSuperX(notification.extras);
            if (TextUtils.isEmpty(titleValue)) {
                titleValue = superXValue(notification.extras, "title");
            }
            if (TextUtils.isEmpty(textValue)) {
                textValue = superXValue(notification.extras, "content");
            }
            String title = clean(titleValue, 220);
            String text = clean(textValue, 500);
            String category = notification.category == null ? "" : notification.category;
            boolean ongoing = (notification.flags & Notification.FLAG_ONGOING_EVENT) != 0;
            boolean groupSummary = (notification.flags & Notification.FLAG_GROUP_SUMMARY) != 0;
            String packageName = sbn.getPackageName();
            boolean noise = NotificationContextPolicy.isNoise(
                    getPackageName(), packageName, category, ongoing, liveCard, title, text);
            if (groupSummary || noise) return;
            String identity = NotificationContextPolicy.identity(packageName, sbn.getKey());

            JSONObject data = new JSONObject();
            data.put("key", identity);
            data.put("package_name", packageName);
            data.put("app_name", appName(packageName));
            data.put("posted_at", sbn.getPostTime() / 1000.0);
            data.put("observed_at", nowMs / 1000.0);
            data.put("title", title);
            data.put("text", text);
            data.put("category", category);
            data.put("channel_id", Build.VERSION.SDK_INT >= 26 ? notification.getChannelId() : "");
            data.put("ongoing", ongoing);
            data.put("group_summary", groupSummary);
            data.put("noise", false);

            String fingerprint = NotificationContextPolicy.fingerprint(
                    title, text, category, ongoing);
            String previousFingerprint = forwardedPayloads.put(identity, fingerprint);
            if (fingerprint.equals(previousFingerprint)) return;

            Intent intent = new Intent(this, AionPushService.class);
            intent.putExtra("action", AionPushService.ACTION_DEVICE_NOTIFICATION_POSTED);
            intent.putExtra(AionPushService.EXTRA_DEVICE_NOTIFICATION_JSON, data.toString());
            try {
                ContextCompat.startForegroundService(this, intent);
                Log.d(TAG, "forwarded package=" + packageName + " live=" + liveCard);
            } catch (RuntimeException error) {
                restoreFingerprint(identity, fingerprint, previousFingerprint);
                throw error;
            }
        } catch (Exception error) {
            Log.d(TAG, "notification extraction skipped: " + error.getMessage());
        }
    }

    @Override public void onNotificationRemoved(StatusBarNotification sbn) {
        if (sbn == null) return;
        String identity = NotificationContextPolicy.identity(sbn.getPackageName(), sbn.getKey());
        if (forwardedPayloads.remove(identity) == null) return;
        Intent intent = new Intent(this, AionPushService.class);
        intent.putExtra("action", AionPushService.ACTION_DEVICE_NOTIFICATION_REMOVED);
        intent.putExtra(AionPushService.EXTRA_DEVICE_NOTIFICATION_KEY,
                identity);
        intent.putExtra(AionPushService.EXTRA_DEVICE_NOTIFICATION_OBSERVED_AT,
                System.currentTimeMillis() / 1000.0);
        ContextCompat.startForegroundService(this, intent);
    }

    private void restoreFingerprint(String identity, String fingerprint, String previous) {
        if (previous == null) {
            forwardedPayloads.remove(identity, fingerprint);
        } else {
            forwardedPayloads.replace(identity, fingerprint, previous);
        }
    }

    private static boolean isSuperX(Bundle extras) {
        if (extras == null) return false;
        for (String key : extras.keySet()) {
            if (key != null && key.startsWith(SUPERX_PREFIX)) return true;
        }
        return false;
    }

    private static CharSequence superXValue(Bundle extras, String field) {
        if (extras == null) return null;
        String[] containers = {
                "notification.superx.baseInfos",
                "notification.superx.capsule"
        };
        for (String container : containers) {
            Object nested = extras.get(container);
            if (nested instanceof Bundle) {
                Object value = ((Bundle) nested).get(field);
                if (value instanceof CharSequence && !TextUtils.isEmpty((CharSequence) value)) {
                    return (CharSequence) value;
                }
            }
        }
        for (String key : extras.keySet()) {
            if (key == null || !key.startsWith(SUPERX_PREFIX) || !key.endsWith("." + field)) {
                continue;
            }
            Object value = extras.get(key);
            if (value instanceof CharSequence && !TextUtils.isEmpty((CharSequence) value)) {
                return (CharSequence) value;
            }
        }
        return null;
    }

    private String appName(String packageName) {
        try {
            return getPackageManager().getApplicationLabel(
                    getPackageManager().getApplicationInfo(packageName, 0)).toString();
        } catch (Exception ignored) {
            return packageName;
        }
    }

    private static String clean(CharSequence value, int limit) {
        if (value == null) return "";
        String text = value.toString().replaceAll("\\s+", " ").trim();
        return text.length() <= limit ? text : text.substring(0, limit);
    }
}
