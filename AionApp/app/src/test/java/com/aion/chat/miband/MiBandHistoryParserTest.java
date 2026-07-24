package com.aion.chat.miband;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.Assert.assertEquals;

public class MiBandHistoryParserTest {
    @Test
    public void keepsValidHeartRatesAndDeduplicatesTimestampAndBpm() {
        List<MiBandProtocol.ActivitySample> input = new ArrayList<>();
        input.add(sample(1_000L, 0, 5));
        input.add(sample(2_000L, 19, 6));
        input.add(sample(3_000L, 71, 7));
        input.add(sample(3_000L, 71, 7));
        input.add(sample(3_000L, 72, 7));
        input.add(sample(4_000L, 241, 8));
        input.add(sample(5_000L, 255, 9));

        List<MiBandHistoryParser.HeartSample> result = MiBandHistoryParser.heartSamples(input);

        assertEquals(2, result.size());
        assertEquals(71, result.get(0).heartRate);
        assertEquals(72, result.get(1).heartRate);
        assertEquals(7, result.get(0).activity.steps);
    }

    @Test
    public void latestCursorAdvancesOneMinuteAfterLastActivityMinute() {
        List<MiBandProtocol.ActivitySample> input = new ArrayList<>();
        input.add(sample(120_000L, 70, 1));
        input.add(sample(180_000L, 0, 2));

        assertEquals(240_000L, MiBandHistoryParser.nextCursor(100_000L, input));
        assertEquals(100_000L, MiBandHistoryParser.nextCursor(100_000L, new ArrayList<>()));
    }

    @Test
    public void firstFullActivitySyncRewindsSevenDaysThenUsesCursor() {
        long now = 10L * 24L * 60L * 60L * 1000L;
        long cursor = now - 60_000L;
        assertEquals(now - 7L * 24L * 60L * 60L * 1000L,
                MiBandHistoryParser.syncStartMillis(now, cursor, false));
        assertEquals(cursor, MiBandHistoryParser.syncStartMillis(now, cursor, true));
    }

    private static MiBandProtocol.ActivitySample sample(long timestamp, int bpm, int steps) {
        return new MiBandProtocol.ActivitySample(
                timestamp, 118, 4, steps, bpm, 0, 0, 0, 0, null);
    }
}
