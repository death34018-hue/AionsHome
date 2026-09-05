package com.aion.chat.widget;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;

import java.util.TimeZone;

final class WidgetRefreshScheduler {
    private WidgetRefreshScheduler() {}

    static void scheduleNext(Context context) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (alarms == null) return;
        long triggerAt = WidgetTimeTheme.nextBoundaryMillis(
                System.currentTimeMillis(), TimeZone.getDefault());
        long bannerExpiry = WidgetBannerState.activeExpiry(context, System.currentTimeMillis());
        if (bannerExpiry > 0L) triggerAt = Math.min(triggerAt, bannerExpiry);
        alarms.setAndAllowWhileIdle(AlarmManager.RTC, triggerAt, pendingIntent(context));
    }

    static void cancel(Context context) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (alarms != null) alarms.cancel(pendingIntent(context));
    }

    private static PendingIntent pendingIntent(Context context) {
        Intent intent = new Intent(context, CompanionWidgetProvider.class)
                .setAction(CompanionWidgetProvider.ACTION_THEME_REFRESH);
        return PendingIntent.getBroadcast(context, 41, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
}
