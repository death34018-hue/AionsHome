package com.aion.chat;

import java.util.Calendar;

/** Schedules failed ring syncs at the next fixed ten-minute slot. */
final class RingSyncSchedule {
    private static final long MINUTE_MS = 60_000L;

    private RingSyncSchedule() {}

    static long alignFailureRetryAt(
            long failedAtMs,
            int offsetMinute,
            long intervalMs) {
        int intervalMinutes = (int) Math.max(1L, intervalMs / MINUTE_MS);
        Calendar slot = Calendar.getInstance();
        slot.setTimeInMillis(failedAtMs);
        int minutesSinceSlot = Math.floorMod(
                slot.get(Calendar.MINUTE) - offsetMinute,
                intervalMinutes);
        slot.add(Calendar.MINUTE, -minutesSinceSlot);
        slot.set(Calendar.SECOND, 0);
        slot.set(Calendar.MILLISECOND, 0);

        long retryAt = slot.getTimeInMillis() + intervalMs;
        while (retryAt <= failedAtMs) retryAt += intervalMs;
        return retryAt;
    }
}
