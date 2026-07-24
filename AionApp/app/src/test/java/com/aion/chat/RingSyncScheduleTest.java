package com.aion.chat;

import static org.junit.Assert.assertEquals;

import java.util.Calendar;

import org.junit.Test;

public class RingSyncScheduleTest {
    private static final long TEN_MINUTES_MS = 10 * 60_000L;

    @Test
    public void firstFailureRetriesAtTheNextScheduledSlotNotTenMinutesAfterScanEnds() {
        long failedAt = localTime(20, 32, 12, 29);

        long retryAt = RingSyncSchedule.alignFailureRetryAt(
                failedAt, 2, TEN_MINUTES_MS);

        assertEquals(localTime(20, 42, 0, 0), retryAt);
    }

    @Test
    public void repeatedFailureStillRetriesAtTheNextScheduledSlot() {
        long failedAt = localTime(20, 42, 12, 29);

        long retryAt = RingSyncSchedule.alignFailureRetryAt(
                failedAt, 2, TEN_MINUTES_MS);

        assertEquals(localTime(20, 52, 0, 0), retryAt);
    }

    private static long localTime(int hour, int minute, int second, int millis) {
        Calendar cal = Calendar.getInstance();
        cal.clear();
        cal.set(2026, Calendar.JULY, 13, hour, minute, second);
        cal.set(Calendar.MILLISECOND, millis);
        return cal.getTimeInMillis();
    }
}
