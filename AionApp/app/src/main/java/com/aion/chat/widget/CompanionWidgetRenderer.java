package com.aion.chat.widget;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.widget.RemoteViews;
import android.view.View;

import com.aion.chat.R;

import java.util.List;

final class CompanionWidgetRenderer {
    private CompanionWidgetRenderer() {}

    static RemoteViews render(Context context) {
        WidgetTimeTheme.Period period = WidgetTimeTheme.currentPeriod();
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_companion);
        String bannerText = WidgetBannerState.current(context, System.currentTimeMillis());
        boolean showBanner = !bannerText.isEmpty();
        views.setViewVisibility(R.id.widget_normal_container, showBanner ? View.GONE : View.VISIBLE);
        views.setViewVisibility(R.id.widget_banner_container, showBanner ? View.VISIBLE : View.GONE);
        views.setTextViewText(R.id.widget_banner_text, bannerText);
        views.setImageViewResource(R.id.widget_background, backgroundFor(period));

        WidgetStateStore widgetState = new WidgetStateStore(context);
        boolean impatient = period == WidgetTimeTheme.Period.MORNING
                || period == WidgetTimeTheme.Period.AFTERNOON;
        if (showBanner) {
            setCachedImage(views, R.id.widget_banner_image,
                    widgetState.bannerAssetPath(), R.drawable.widget_banner);
        } else {
            setCachedImage(views, R.id.widget_companion_left,
                    widgetState.assetPath("connor"), impatient
                            ? R.drawable.widget_companion_left_impatient
                            : R.drawable.widget_companion_left_calm);
            setCachedImage(views, R.id.widget_companion_right,
                    widgetState.assetPath("aion"), impatient
                            ? R.drawable.widget_companion_right_impatient
                            : R.drawable.widget_companion_right_calm);
        }
        List<PrivateMemo> memos = new PrivateMemoStore(context).latest(2);
        WidgetMemoPresentation memoView = WidgetMemoPresentation.from(memos);
        views.setViewVisibility(R.id.widget_empty_hint,
                memoView.emptyHintVisible ? View.VISIBLE : View.GONE);
        views.setViewVisibility(R.id.widget_memo_first,
                memoView.firstVisible ? View.VISIBLE : View.GONE);
        views.setViewVisibility(R.id.widget_memo_second,
                memoView.secondVisible ? View.VISIBLE : View.GONE);
        views.setTextViewText(R.id.widget_memo_first, memoView.firstText);
        views.setTextViewText(R.id.widget_memo_second, memoView.secondText);

        Intent recordIntent = new Intent();
        recordIntent.setClassName(context.getPackageName(),
                "com.aion.chat.widget.WidgetRecordActivity");
        recordIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                context, 0, recordIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widget_microphone, pendingIntent);
        views.setOnClickPendingIntent(R.id.widget_companion_left,
                tapIntent(context, CompanionWidgetProvider.ACTION_WIDGET_TAP_LEFT, 51));
        views.setOnClickPendingIntent(R.id.widget_companion_right,
                tapIntent(context, CompanionWidgetProvider.ACTION_WIDGET_TAP_RIGHT, 52));
        views.setOnClickPendingIntent(R.id.widget_banner_container,
                tapIntent(context, CompanionWidgetProvider.ACTION_WIDGET_TAP_BANNER, 53));
        return views;
    }

    private static void setCachedImage(RemoteViews views, int viewId,
                                       String path, int fallbackResource) {
        Bitmap bitmap = path == null || path.isEmpty() ? null : BitmapFactory.decodeFile(path);
        if (bitmap != null) views.setImageViewBitmap(viewId, bitmap);
        else views.setImageViewResource(viewId, fallbackResource);
    }

    private static PendingIntent tapIntent(Context context, String action, int requestCode) {
        Intent intent = new Intent(context, CompanionWidgetProvider.class).setAction(action);
        return PendingIntent.getBroadcast(context, requestCode, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static int backgroundFor(WidgetTimeTheme.Period period) {
        switch (period) {
            case DAWN: return R.drawable.widget_bg_dawn;
            case NOON: return R.drawable.widget_bg_noon;
            case AFTERNOON: return R.drawable.widget_bg_afternoon;
            case NIGHT: return R.drawable.widget_bg_night;
            default: return R.drawable.widget_bg_morning;
        }
    }
}
