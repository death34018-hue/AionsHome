package com.aion.chat.homecoming;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.os.Build;

import androidx.core.app.NotificationCompat;

public final class HomecomingNotificationController {
    static final String BACKGROUND_CHANNEL = "homecoming_background";
    static final String ALERT_CHANNEL = "homecoming_alerts";

    private final Context context;
    private final NotificationManager manager;

    public HomecomingNotificationController(Context context) {
        if (context == null) throw new IllegalArgumentException("context is required");
        this.context = context.getApplicationContext();
        this.manager = (NotificationManager) this.context.getSystemService(
                Context.NOTIFICATION_SERVICE);
        ensureChannels();
    }

    public Notification backgroundNotification() {
        return build(backgroundProjection(), BACKGROUND_CHANNEL);
    }

    public void post(
            HomecomingScheduleRepository.Schedule schedule,
            String configuredName,
            String text) {
        if (manager == null) return;
        Projection projection = projection(schedule, configuredName, text);
        manager.notify(HomecomingAlarmRegistrar.requestCode(schedule.id),
                build(projection, ALERT_CHANNEL));
    }

    public void post(String eventId, String configuredName, String text) {
        if (manager == null) return;
        Projection projection = new Projection(
                required(configuredName, "configuredName"),
                required(text, "text"),
                true,
                true,
                false);
        manager.notify(
                HomecomingAlarmRegistrar.requestCode(required(eventId, "eventId")),
                build(projection, ALERT_CHANNEL));
    }

    static Projection projection(
            HomecomingScheduleRepository.Schedule schedule,
            String configuredName,
            String text) {
        if (schedule == null) throw new IllegalArgumentException("schedule is required");
        String title = required(configuredName, "configuredName");
        String content = required(text, "text");
        return new Projection(title, content, true, true, false);
    }

    static Projection backgroundProjection() {
        return new Projection(
                "归巢模式", "正在处理归巢后台任务", false, false, true);
    }

    private Notification build(Projection projection, String channel) {
        NotificationCompat.Builder builder = new NotificationCompat.Builder(
                context, channel)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle(projection.title)
                .setContentText(projection.text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(projection.text))
                .setOngoing(projection.ongoing)
                .setOnlyAlertOnce(projection.ongoing)
                .setPriority(projection.highPriority
                        ? NotificationCompat.PRIORITY_HIGH
                        : NotificationCompat.PRIORITY_LOW);
        if (!projection.audible) builder.setSilent(true);
        return builder.build();
    }

    private void ensureChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O || manager == null) return;
        NotificationChannel background = new NotificationChannel(
                BACKGROUND_CHANNEL, "归巢后台任务", NotificationManager.IMPORTANCE_LOW);
        background.setDescription("仅在归巢模式执行灾备后台任务");
        background.setSound(null, null);
        manager.createNotificationChannel(background);

        NotificationChannel alerts = new NotificationChannel(
                ALERT_CHANNEL, "归巢闹铃与提醒", NotificationManager.IMPORTANCE_HIGH);
        alerts.setDescription("归巢模式下原有闹铃、提醒和监督消息");
        manager.createNotificationChannel(alerts);
    }

    public static final class Projection {
        public final String title;
        public final String text;
        public final boolean highPriority;
        public final boolean audible;
        public final boolean ongoing;

        Projection(
                String title,
                String text,
                boolean highPriority,
                boolean audible,
                boolean ongoing) {
            this.title = title;
            this.text = text;
            this.highPriority = highPriority;
            this.audible = audible;
            this.ongoing = ongoing;
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
