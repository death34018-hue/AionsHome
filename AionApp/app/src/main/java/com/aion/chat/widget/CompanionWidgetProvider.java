package com.aion.chat.widget;

import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;

public final class CompanionWidgetProvider extends AppWidgetProvider {
    static final String ACTION_THEME_REFRESH =
            "com.aion.chat.widget.ACTION_THEME_REFRESH";
    static final String ACTION_WIDGET_TAP_LEFT =
            "com.aion.chat.widget.ACTION_WIDGET_TAP_LEFT";
    static final String ACTION_WIDGET_TAP_RIGHT =
            "com.aion.chat.widget.ACTION_WIDGET_TAP_RIGHT";
    static final String ACTION_WIDGET_TAP_BANNER =
            "com.aion.chat.widget.ACTION_WIDGET_TAP_BANNER";

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        for (int id : appWidgetIds) {
            manager.updateAppWidget(id, CompanionWidgetRenderer.render(context));
        }
        WidgetRefreshScheduler.scheduleNext(context);
        WidgetStateSyncClient.sync(context);
    }

    @Override
    public void onAppWidgetOptionsChanged(Context context, AppWidgetManager manager,
                                          int appWidgetId, android.os.Bundle newOptions) {
        manager.updateAppWidget(appWidgetId, CompanionWidgetRenderer.render(context));
        WidgetStateSyncClient.sync(context);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        String action = intent.getAction();
        if (ACTION_WIDGET_TAP_LEFT.equals(action)) {
            WidgetInteractionController.onTap(context, WidgetInteractionController.TARGET_LEFT);
        } else if (ACTION_WIDGET_TAP_RIGHT.equals(action)) {
            WidgetInteractionController.onTap(context, WidgetInteractionController.TARGET_RIGHT);
        } else if (ACTION_WIDGET_TAP_BANNER.equals(action)) {
            WidgetInteractionController.onTap(context, WidgetInteractionController.TARGET_BANNER);
        } else if (ACTION_THEME_REFRESH.equals(action)
                || Intent.ACTION_TIME_CHANGED.equals(action)
                || Intent.ACTION_TIMEZONE_CHANGED.equals(action)) {
            refreshAll(context);
            WidgetRefreshScheduler.scheduleNext(context);
        }
    }

    @Override
    public void onEnabled(Context context) {
        WidgetRefreshScheduler.scheduleNext(context);
        WidgetStateSyncClient.sync(context);
    }

    @Override
    public void onDisabled(Context context) {
        WidgetRefreshScheduler.cancel(context);
    }

    public static void refreshAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        ComponentName provider = new ComponentName(context, CompanionWidgetProvider.class);
        int[] ids = manager.getAppWidgetIds(provider);
        for (int id : ids) {
            manager.updateAppWidget(id, CompanionWidgetRenderer.render(context));
        }
    }
}
