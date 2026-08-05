package com.aion.chat.homecoming;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

public final class HomecomingAlarmRegistrar
        implements HomecomingScheduleCommandHandler.RegistrationPort {
    static final String EXTRA_SCHEDULE_ID = "homecoming_schedule_id";
    static final String EXTRA_TRIGGER_AT = "homecoming_trigger_at";
    static final String EXTRA_EPOCH_ID = "homecoming_epoch_id";

    private final Platform platform;
    private final String epochId;

    public HomecomingAlarmRegistrar(Context context, String epochId) {
        this(new AndroidPlatform(context), epochId);
    }

    HomecomingAlarmRegistrar(Platform platform, String epochId) {
        if (platform == null) throw new IllegalArgumentException("platform is required");
        if (epochId == null || epochId.trim().isEmpty()) {
            throw new IllegalArgumentException("epochId is required");
        }
        this.platform = platform;
        this.epochId = epochId.trim();
    }

    @Override
    public void register(HomecomingScheduleRepository.Schedule schedule) {
        if (schedule == null || !"active".equals(schedule.status)) {
            throw new IllegalArgumentException("active schedule is required");
        }
        boolean exact = !platform.exactPermissionRequired()
                || platform.canScheduleExact();
        platform.schedule(
                requestCode(schedule.id), schedule.triggerAt, exact,
                schedule.id, epochId);
    }

    @Override
    public void cancel(String scheduleId) {
        String checked = required(scheduleId);
        platform.cancel(requestCode(checked), checked, epochId);
    }

    public String exactness() {
        if (!platform.exactPermissionRequired() || platform.canScheduleExact()) {
            return "exact";
        }
        return "permission_required";
    }

    static int requestCode(String scheduleId) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(
                    required(scheduleId).getBytes(StandardCharsets.UTF_8));
            int value = ((digest[0] & 0xff) << 24)
                    | ((digest[1] & 0xff) << 16)
                    | ((digest[2] & 0xff) << 8)
                    | (digest[3] & 0xff);
            return value & 0x7fffffff;
        } catch (Exception exception) {
            throw new IllegalStateException("could not derive alarm identity", exception);
        }
    }

    interface Platform {
        boolean exactPermissionRequired();
        boolean canScheduleExact();
        void schedule(
                int requestCode, long triggerAt, boolean exact,
                String scheduleId, String epochId);
        void cancel(int requestCode, String scheduleId, String epochId);
    }

    private static final class AndroidPlatform implements Platform {
        private final Context context;
        private final AlarmManager alarms;

        AndroidPlatform(Context context) {
            if (context == null) throw new IllegalArgumentException("context is required");
            this.context = context.getApplicationContext();
            this.alarms = (AlarmManager) this.context.getSystemService(
                    Context.ALARM_SERVICE);
            if (alarms == null) throw new IllegalStateException("AlarmManager unavailable");
        }

        @Override public boolean exactPermissionRequired() {
            return Build.VERSION.SDK_INT >= Build.VERSION_CODES.S;
        }

        @Override public boolean canScheduleExact() {
            return Build.VERSION.SDK_INT < Build.VERSION_CODES.S
                    || alarms.canScheduleExactAlarms();
        }

        @Override
        public void schedule(
                int requestCode, long triggerAt, boolean exact,
                String scheduleId, String epochId) {
            PendingIntent intent = pending(
                    context, requestCode, scheduleId, triggerAt, epochId);
            if (exact) {
                alarms.setExactAndAllowWhileIdle(
                        AlarmManager.RTC_WAKEUP, triggerAt, intent);
            } else {
                alarms.setAndAllowWhileIdle(
                        AlarmManager.RTC_WAKEUP, triggerAt, intent);
            }
        }

        @Override
        public void cancel(int requestCode, String scheduleId, String epochId) {
            PendingIntent intent = pending(
                    context, requestCode, scheduleId, 0L, epochId);
            alarms.cancel(intent);
            intent.cancel();
        }

        private static PendingIntent pending(
                Context context, int requestCode, String scheduleId,
                long triggerAt, String epochId) {
            Intent intent = new Intent(context, HomecomingAlarmReceiver.class)
                    .setAction(HomecomingForegroundService.ACTION_FIRE)
                    .putExtra(EXTRA_SCHEDULE_ID, scheduleId)
                    .putExtra(EXTRA_TRIGGER_AT, triggerAt)
                    .putExtra(EXTRA_EPOCH_ID, epochId);
            return PendingIntent.getBroadcast(
                    context,
                    requestCode,
                    intent,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        }
    }

    private static String required(String value) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("scheduleId is required");
        }
        return value.trim();
    }
}
