package com.aion.chat.miband;

import java.util.Calendar;

public final class MiBandSyncSchedule {
    private static final long[] RECONNECT_DELAYS = {
            5_000L, 15_000L, 30_000L, 60_000L, 3_600_000L
    };

    private MiBandSyncSchedule() {}

    public static int intervalMinutes(Calendar now, MiBandPreferences.Settings settings) {
        int minute = now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE);
        return isNight(minute, settings)
                ? settings.nightIntervalMinutes
                : settings.dayIntervalMinutes;
    }

    public static long nextDelayMillis(Calendar now, MiBandPreferences.Settings settings) {
        int interval = intervalMinutes(now, settings);
        if (interval < 0) return -1L;
        long intervalMillis = interval * 60_000L;
        long nextInterval = ((now.getTimeInMillis() / intervalMillis) + 1L) * intervalMillis;
        long nextBoundary = nextWindowBoundaryMillis(now, settings);
        long target = Math.min(nextInterval, nextBoundary);
        return Math.max(1_000L, target - now.getTimeInMillis());
    }

    public static long reconnectDelayMillis(int failureIndex) {
        int index = Math.max(0, Math.min(failureIndex, RECONNECT_DELAYS.length - 1));
        return RECONNECT_DELAYS[index];
    }

    private static boolean isNight(int minute, MiBandPreferences.Settings settings) {
        int start = settings.nightStartMinute;
        int end = settings.nightEndMinute;
        if (start == end) return false;
        if (start < end) return minute >= start && minute < end;
        return minute >= start || minute < end;
    }

    private static long nextWindowBoundaryMillis(Calendar now, MiBandPreferences.Settings settings) {
        int minute = now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE);
        boolean night = isNight(minute, settings);
        int boundaryMinute = night ? settings.nightEndMinute : settings.nightStartMinute;
        Calendar boundary = (Calendar) now.clone();
        boundary.set(Calendar.HOUR_OF_DAY, boundaryMinute / 60);
        boundary.set(Calendar.MINUTE, boundaryMinute % 60);
        boundary.set(Calendar.SECOND, 0);
        boundary.set(Calendar.MILLISECOND, 0);
        if (boundary.getTimeInMillis() <= now.getTimeInMillis()) {
            boundary.add(Calendar.DAY_OF_MONTH, 1);
        }
        return boundary.getTimeInMillis();
    }
}
