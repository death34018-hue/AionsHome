package com.aion.chat.miband;

import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

public final class MiBandPreferences {
    public static final String PREFS_NAME = "aion_mi_band_7";
    public static final String KEY_ADDRESS = "device_address";
    public static final String KEY_NAME = "device_name";
    public static final String KEY_AUTH = "auth_key_hex";
    public static final String KEY_DAY_INTERVAL = "day_interval_minutes";
    public static final String KEY_NIGHT_START = "night_start_minute";
    public static final String KEY_NIGHT_END = "night_end_minute";
    public static final String KEY_NIGHT_INTERVAL = "night_interval_minutes";
    public static final String KEY_ACTIVITY_CURSOR = "activity_cursor_ms";
    public static final String KEY_FULL_ACTIVITY_SYNCED = "full_activity_synced_v1";
    public static final String KEY_RECONNECT_FAILURES = "reconnect_failures";
    public static final String KEY_NEXT_RECONNECT_AT = "next_reconnect_at";
    private static final Pattern CLOCK = Pattern.compile("^[0-2][0-9]:[0-5][0-9]$");

    private MiBandPreferences() {}

    public static int requireInterval(int value) {
        if (value == -1 || value == 1 || value == 5 || value == 10
                || value == 20 || value == 30 || value == 60) return value;
        throw new IllegalArgumentException("unsupported sync interval");
    }

    public static int parseClock(String value) {
        if (value == null || !CLOCK.matcher(value).matches()) {
            throw new IllegalArgumentException("invalid time");
        }
        int hour = Integer.parseInt(value.substring(0, 2));
        int minute = Integer.parseInt(value.substring(3, 5));
        if (hour > 23) throw new IllegalArgumentException("invalid time");
        return hour * 60 + minute;
    }

    public static String formatClock(int minuteOfDay) {
        requireMinute(minuteOfDay);
        return String.format(Locale.US, "%02d:%02d", minuteOfDay / 60, minuteOfDay % 60);
    }

    public static byte[] decodeAuthKey(String value) {
        String normalized = value == null ? "" : value.trim();
        if (!normalized.matches("(?i)^[0-9a-f]{32}$")) {
            throw new IllegalArgumentException("auth key must be 32 hexadecimal characters");
        }
        byte[] out = new byte[16];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(normalized.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    public static Map<String, Object> publicConfig(String address, String name,
                                                    String authKeyHex, Settings settings) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("device_name", name == null ? "" : name);
        out.put("masked_address", maskAddress(address));
        out.put("auth_key_configured", authKeyHex != null && authKeyHex.matches("(?i)^[0-9a-f]{32}$"));
        out.put("day_interval_minutes", settings.dayIntervalMinutes);
        out.put("night_start", formatClock(settings.nightStartMinute));
        out.put("night_end", formatClock(settings.nightEndMinute));
        out.put("night_interval_minutes", settings.nightIntervalMinutes);
        return out;
    }

    public static String maskAddress(String value) {
        if (value == null || value.isEmpty()) return "";
        String[] parts = value.split(":");
        if (parts.length != 6) return "**:**:**:**:**:**";
        return parts[0] + ":" + parts[1] + ":**:**:" + parts[4] + ":" + parts[5];
    }

    private static int requireMinute(int value) {
        if (value < 0 || value >= 24 * 60) throw new IllegalArgumentException("invalid time minute");
        return value;
    }

    public static final class Settings {
        public final int dayIntervalMinutes;
        public final int nightStartMinute;
        public final int nightEndMinute;
        public final int nightIntervalMinutes;

        public Settings(int dayIntervalMinutes, int nightStartMinute,
                        int nightEndMinute, int nightIntervalMinutes) {
            this.dayIntervalMinutes = requireInterval(dayIntervalMinutes);
            this.nightStartMinute = requireMinute(nightStartMinute);
            this.nightEndMinute = requireMinute(nightEndMinute);
            this.nightIntervalMinutes = requireInterval(nightIntervalMinutes);
        }

        public static Settings defaults() {
            return new Settings(1, 2 * 60, 8 * 60, 20);
        }
    }
}
