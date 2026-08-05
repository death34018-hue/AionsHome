package com.aion.chat.homecoming;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

public final class HomecomingBootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (context == null || !new HomecomingModeStore(context).isActive()) return;
        Intent service = new Intent(context, HomecomingForegroundService.class)
                .setAction(HomecomingForegroundService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(service);
        } else {
            context.startService(service);
        }
    }
}
