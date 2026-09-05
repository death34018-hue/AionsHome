package com.aion.chat.widget;

import java.util.Calendar;
import java.util.TimeZone;

public final class WidgetTimeTheme {
    public enum Period { DAWN, MORNING, NOON, AFTERNOON, NIGHT }

    private static final int DAWN_START = 5 * 60;
    private static final int MORNING_START = 8 * 60 + 30;
    private static final int NOON_START = 11 * 60 + 30;
    private static final int AFTERNOON_START = 14 * 60;
    private static final int NIGHT_START = 18 * 60;

    private WidgetTimeTheme() {}

    public static Period periodForMinutes(int minutes) {
        int normalized = ((minutes % (24 * 60)) + 24 * 60) % (24 * 60);
        if (normalized < DAWN_START || normalized >= NIGHT_START) return Period.NIGHT;
        if (normalized < MORNING_START) return Period.DAWN;
        if (normalized < NOON_START) return Period.MORNING;
        if (normalized < AFTERNOON_START) return Period.NOON;
        return Period.AFTERNOON;
    }

    public static Period currentPeriod() {
        Calendar now = Calendar.getInstance();
        return periodForMinutes(now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE));
    }

    public static long nextBoundaryMillis(long nowMillis, TimeZone zone) {
        Calendar now = Calendar.getInstance(zone);
        now.setTimeInMillis(nowMillis);
        int minutes = now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE);
        int nextMinutes;
        if (minutes < DAWN_START) nextMinutes = DAWN_START;
        else if (minutes < MORNING_START) nextMinutes = MORNING_START;
        else if (minutes < NOON_START) nextMinutes = NOON_START;
        else if (minutes < AFTERNOON_START) nextMinutes = AFTERNOON_START;
        else if (minutes < NIGHT_START) nextMinutes = NIGHT_START;
        else nextMinutes = DAWN_START + 24 * 60;

        Calendar boundary = (Calendar) now.clone();
        boundary.set(Calendar.HOUR_OF_DAY, 0);
        boundary.set(Calendar.MINUTE, 0);
        boundary.set(Calendar.SECOND, 0);
        boundary.set(Calendar.MILLISECOND, 0);
        if (nextMinutes >= 24 * 60) {
            boundary.add(Calendar.DAY_OF_MONTH, 1);
            nextMinutes -= 24 * 60;
        }
        boundary.add(Calendar.MINUTE, nextMinutes);
        return boundary.getTimeInMillis();
    }
}
