package com.aion.chat.miband;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class MiBandHistoryParser {
    private MiBandHistoryParser() {}

    public static List<HeartSample> heartSamples(List<MiBandProtocol.ActivitySample> activity) {
        List<HeartSample> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (MiBandProtocol.ActivitySample item : activity) {
            if (item.heartRate < 20 || item.heartRate > 240) continue;
            String key = item.timestampMillis + ":" + item.heartRate;
            if (seen.add(key)) out.add(new HeartSample(item.timestampMillis, item.heartRate, item));
        }
        return out;
    }

    public static long nextCursor(long current, List<MiBandProtocol.ActivitySample> activity) {
        long latest = current - 60_000L;
        for (MiBandProtocol.ActivitySample item : activity) {
            latest = Math.max(latest, item.timestampMillis);
        }
        return Math.max(current, latest + 60_000L);
    }

    public static long syncStartMillis(long now, long cursor, boolean fullActivitySynced) {
        return fullActivitySynced
                ? cursor
                : now - 7L * 24L * 60L * 60L * 1000L;
    }

    public static final class HeartSample {
        public final long timestampMillis;
        public final int heartRate;
        public final MiBandProtocol.ActivitySample activity;

        HeartSample(long timestampMillis, int heartRate, MiBandProtocol.ActivitySample activity) {
            this.timestampMillis = timestampMillis;
            this.heartRate = heartRate;
            this.activity = activity;
        }
    }
}
