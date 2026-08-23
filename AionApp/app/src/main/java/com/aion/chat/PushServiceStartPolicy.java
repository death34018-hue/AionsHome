package com.aion.chat;

final class PushServiceStartPolicy {
    static final String ACTION_SET_FOREGROUND = "set_foreground";
    static final String PREF_LAST_ACTIVE_URL = "last_active_url";

    private PushServiceStartPolicy() {}

    static boolean canReturnAfterLightweightAction(
            String action,
            String serverUrl,
            boolean heartbeatAlive
    ) {
        if (ACTION_SET_FOREGROUND.equals(action)
                || AionPushService.ACTION_REFRESH_CLOUDFLARE_AUTH.equals(action)) {
            return hasText(serverUrl) && heartbeatAlive;
        }
        return true;
    }

    static String chooseFallbackPageUrl(
            String lastActiveUrl,
            String rememberedUrl,
            String defaultUrl
    ) {
        if (hasText(lastActiveUrl)) return lastActiveUrl.trim();
        if (hasText(rememberedUrl)) return rememberedUrl.trim();
        return defaultUrl;
    }

    static String keepAliveText(boolean connected) {
        return connected ? "在线 ✨" : "连接中...";
    }

    private static boolean hasText(String value) {
        return value != null && !value.trim().isEmpty();
    }
}
