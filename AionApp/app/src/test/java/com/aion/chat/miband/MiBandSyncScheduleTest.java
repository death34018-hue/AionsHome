package com.aion.chat.miband;

import org.junit.Test;

import java.util.Calendar;
import java.util.TimeZone;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class MiBandSyncScheduleTest {
    private static final TimeZone CHINA = TimeZone.getTimeZone("GMT+08:00");

    @Test
    public void defaultWindowSwitchesAtTwoAndEight() {
        MiBandPreferences.Settings settings = MiBandPreferences.Settings.defaults();

        assertEquals(1, MiBandSyncSchedule.intervalMinutes(at(1, 59), settings));
        assertEquals(20, MiBandSyncSchedule.intervalMinutes(at(2, 0), settings));
        assertEquals(20, MiBandSyncSchedule.intervalMinutes(at(7, 59), settings));
        assertEquals(1, MiBandSyncSchedule.intervalMinutes(at(8, 0), settings));
    }

    @Test
    public void supportsNightWindowCrossingMidnight() {
        MiBandPreferences.Settings settings = new MiBandPreferences.Settings(5, 23 * 60, 7 * 60, 30);

        assertEquals(30, MiBandSyncSchedule.intervalMinutes(at(23, 30), settings));
        assertEquals(30, MiBandSyncSchedule.intervalMinutes(at(6, 59), settings));
        assertEquals(5, MiBandSyncSchedule.intervalMinutes(at(12, 0), settings));
    }

    @Test
    public void pausedPeriodReturnsNoScheduledDelay() {
        MiBandPreferences.Settings settings = new MiBandPreferences.Settings(-1, 2 * 60, 8 * 60, 20);

        assertEquals(-1, MiBandSyncSchedule.intervalMinutes(at(12, 0), settings));
        assertEquals(-1L, MiBandSyncSchedule.nextDelayMillis(at(12, 0), settings));
    }

    @Test
    public void delayAlignsToNextIntervalBoundary() {
        MiBandPreferences.Settings settings = new MiBandPreferences.Settings(5, 2 * 60, 8 * 60, 20);
        Calendar current = at(12, 3);
        current.set(Calendar.SECOND, 10);

        long delay = MiBandSyncSchedule.nextDelayMillis(current, settings);

        assertEquals(110_000L, delay);
    }

    @Test
    public void reconnectBackoffIsBounded() {
        long[] expected = {5_000L, 15_000L, 30_000L, 60_000L, 3_600_000L, 3_600_000L};
        for (int i = 0; i < expected.length; i++) {
            assertEquals(expected[i], MiBandSyncSchedule.reconnectDelayMillis(i));
        }
        assertTrue(MiBandSyncSchedule.reconnectDelayMillis(100) <= 3_600_000L);
    }

    private static Calendar at(int hour, int minute) {
        Calendar value = Calendar.getInstance(CHINA);
        value.clear();
        value.set(2026, Calendar.JULY, 17, hour, minute, 0);
        return value;
    }
}
