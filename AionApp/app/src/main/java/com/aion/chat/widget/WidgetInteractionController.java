package com.aion.chat.widget;

import android.content.Context;
import android.content.SharedPreferences;

final class WidgetInteractionController {
    static final long DOUBLE_TAP_MILLIS = 450L;
    static final String TARGET_LEFT = "left";
    static final String TARGET_RIGHT = "right";
    static final String TARGET_BANNER = "banner";

    private static final String PREFS = "widget_taps";

    private WidgetInteractionController() {}

    static void onTap(Context context, String target) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        long now = System.currentTimeMillis();
        long lastAt = prefs.getLong("last_at", 0L);
        String lastTarget = prefs.getString("last_target", "");
        if (!target.equals(lastTarget) || now - lastAt > DOUBLE_TAP_MILLIS) {
            prefs.edit().putLong("last_at", now).putString("last_target", target).apply();
            return;
        }
        prefs.edit().clear().apply();

        WidgetStateStore state = new WidgetStateStore(context);
        if (TARGET_BANNER.equals(target)) {
            WidgetBannerState.clear(context);
            state.queueBannerClear();
        } else {
            String actorId = TARGET_LEFT.equals(target) ? "connor" : "aion";
            String next = state.nextState(actorId);
            if (!next.isEmpty()) state.queueActorState(actorId, next);
        }
        WidgetStateSyncClient.sync(context);
    }
}
