package com.aion.chat;

import java.util.Locale;

/** Cheap phone-side gate; the server still ranks and clips the remaining notifications. */
public final class NotificationContextPolicy {
    private static final long FRESH_WINDOW_MS = 30 * 60_000L;
    private NotificationContextPolicy() {}

    public static boolean isNoise(String ownPackage, String packageName, String category,
                                  boolean ongoing, String title, String text) {
        return isNoise(ownPackage, packageName, category, ongoing, false, title, text);
    }

    public static boolean isNoise(String ownPackage, String packageName, String category,
                                  boolean ongoing, boolean liveCard, String title, String text) {
        String pkg = safe(packageName);
        String kind = safe(category).toLowerCase(Locale.US);
        String content = (safe(title) + " " + safe(text)).toLowerCase(Locale.US);
        if (pkg.equals(safe(ownPackage))) return true;
        if (liveCard || isImportantLiveStatus(content)) return false;
        if ("transport".equals(kind) || "service".equals(kind) || "progress".equals(kind)) {
            return true;
        }
        return ongoing && (content.contains("运行中") || content.contains("播放中")
                || content.contains("running") || content.contains("playing"));
    }

    public static String identity(String packageName, String key) {
        return safe(packageName) + ":" + safe(key);
    }

    public static String liveIdentity(String packageName, String scene) {
        String normalizedScene = normalize(scene).toLowerCase(Locale.US);
        if (normalizedScene.isEmpty()) normalizedScene = "status";
        return identity(packageName, "accessibility-live:" + normalizedScene);
    }

    public static boolean isImportantLiveStatus(String title, String text) {
        return isImportantLiveStatus((safe(title) + " " + safe(text)).toLowerCase(Locale.US));
    }

    public static boolean shouldForwardAccessibility(boolean liveCard, String title, String text) {
        return liveCard && (!safe(title).isEmpty() || !safe(text).isEmpty());
    }

    public static String fingerprint(String title, String text, String category, boolean ongoing) {
        return normalize(title) + "\n" + normalize(text) + "\n"
                + normalize(category).toLowerCase(Locale.US) + "\n" + ongoing;
    }

    public static boolean isFresh(long postedAtMs, long nowMs) {
        return postedAtMs > 0 && postedAtMs <= nowMs + 60_000L
                && nowMs - postedAtMs <= FRESH_WINDOW_MS;
    }

    private static boolean isImportantLiveStatus(String content) {
        String[] promotions = {
                "红包", "优惠", "折扣", "促销", "大促", "领券", "购买", "下单更",
                "coupon", "discount", "limited offer", "sale"
        };
        for (String promotion : promotions) {
            if (content.contains(promotion)) return false;
        }
        String[] markers = {
                "外卖", "配送", "骑手", "骑士", "送达", "取餐",
                "快递", "包裹", "派送", "驿站",
                "打车", "司机", "网约车",
                "航班", "登机", "候机", "起飞", "延误",
                "delivery", "courier", "parcel", "driver", "taxi", "flight", "boarding"
        };
        for (String marker : markers) {
            if (content.contains(marker)) return true;
        }
        return false;
    }

    private static String normalize(String value) {
        return safe(value).replaceAll("\\s+", " ");
    }

    private static String safe(String value) { return value == null ? "" : value.trim(); }
}
