package com.aion.chat.miband;

import org.junit.Test;

import java.util.Map;

import static org.junit.Assert.*;

public class MiBandPreferencesTest {
    @Test
    public void defaultsMatchApprovedDayAndNightSchedule() {
        MiBandPreferences.Settings settings = MiBandPreferences.Settings.defaults();

        assertEquals(1, settings.dayIntervalMinutes);
        assertEquals(120, settings.nightStartMinute);
        assertEquals(480, settings.nightEndMinute);
        assertEquals(20, settings.nightIntervalMinutes);
    }

    @Test
    public void acceptsOnlyApprovedSyncIntervals() {
        int[] valid = {-1, 1, 5, 10, 20, 30, 60};
        for (int value : valid) assertEquals(value, MiBandPreferences.requireInterval(value));
        try {
            MiBandPreferences.requireInterval(2);
            fail("expected invalid interval");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("interval"));
        }
    }

    @Test
    public void parsesClockAndRejectsInvalidValues() {
        assertEquals(125, MiBandPreferences.parseClock("02:05"));
        assertEquals("02:05", MiBandPreferences.formatClock(125));
        for (String invalid : new String[] {"2:05", "24:00", "02:60", "nope"}) {
            try {
                MiBandPreferences.parseClock(invalid);
                fail("expected invalid clock: " + invalid);
            } catch (IllegalArgumentException expected) {
                assertTrue(expected.getMessage().contains("time"));
            }
        }
    }

    @Test
    public void validatesAndDecodesSixteenByteAuthKey() {
        byte[] decoded = MiBandPreferences.decodeAuthKey("00112233445566778899AABBCCDDEEFF");

        assertEquals(16, decoded.length);
        assertEquals(0xff, decoded[15] & 0xff);
    }

    @Test(expected = IllegalArgumentException.class)
    public void rejectsMalformedAuthKey() {
        MiBandPreferences.decodeAuthKey("0011-secret");
    }

    @Test
    public void publicConfigNeverContainsAuthKey() {
        Map<String, Object> value = MiBandPreferences.publicConfig(
                "CC:74:25:50:2F:26",
                "Xiaomi Smart Band 7",
                "00112233445566778899aabbccddeeff",
                MiBandPreferences.Settings.defaults());

        assertEquals(Boolean.TRUE, value.get("auth_key_configured"));
        assertFalse(value.containsKey("auth_key"));
        assertFalse(value.toString().contains("00112233445566778899aabbccddeeff"));
        assertEquals("CC:74:**:**:2F:26", value.get("masked_address"));
    }
}
