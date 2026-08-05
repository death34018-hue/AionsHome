package com.aion.chat.homecoming;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

public final class HomecomingAlarmReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (context == null || intent == null) return;
        HomecomingModeStore mode = new HomecomingModeStore(context);
        String epochId = intent.getStringExtra(HomecomingAlarmRegistrar.EXTRA_EPOCH_ID);
        if (!mode.isActive() || epochId == null
                || !epochId.equals(mode.currentEpoch())) {
            return;
        }
        Intent service = new Intent(context, HomecomingForegroundService.class)
                .setAction(HomecomingForegroundService.ACTION_FIRE)
                .putExtra(
                        HomecomingAlarmRegistrar.EXTRA_SCHEDULE_ID,
                        intent.getStringExtra(
                                HomecomingAlarmRegistrar.EXTRA_SCHEDULE_ID))
                .putExtra(
                        HomecomingAlarmRegistrar.EXTRA_TRIGGER_AT,
                        intent.getLongExtra(
                                HomecomingAlarmRegistrar.EXTRA_TRIGGER_AT, 0L))
                .putExtra(HomecomingAlarmRegistrar.EXTRA_EPOCH_ID, epochId);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(service);
        } else {
            context.startService(service);
        }
    }
}
