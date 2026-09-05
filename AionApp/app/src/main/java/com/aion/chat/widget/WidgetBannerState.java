package com.aion.chat.widget;

import android.content.Context;
import android.content.SharedPreferences;

public final class WidgetBannerState {
    private static final String PREFS = "widget_banner_state";
    private static final String KEY_TEXT = "text";

    private WidgetBannerState() {}

    public static void show(Context context, String text) {
        String value = text == null ? "" : text.trim();
        if (value.isEmpty()) {
            clear(context);
            return;
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString(KEY_TEXT, value)
                .apply();
        CompanionWidgetProvider.refreshAll(context);
        WidgetRefreshScheduler.scheduleNext(context);
    }

    public static void clear(Context context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply();
        CompanionWidgetProvider.refreshAll(context);
        WidgetRefreshScheduler.scheduleNext(context);
    }

    static String current(Context context, long nowMillis) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String text = prefs.getString(KEY_TEXT, "");
        return text == null ? "" : text.trim();
    }

    static long activeExpiry(Context context, long nowMillis) {
        return 0L;
    }
}
