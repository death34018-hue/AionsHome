package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public class RingHealthSnapshotTest {
    @Test
    public void parsesLatestComprehensiveRecordUsingHealthPageLayout() {
        byte[] payload = new byte[40];
        putRecord(payload, 0, 100, 1234, 61, 112, 72, 97, 15, 42);
        putRecord(payload, 20, 200, 4321, 68, 118, 76, 99, 16, 51);

        RingHealthSnapshot snapshot =
                RingHealthSnapshot.latestFromPayload(payload, 946684800.0);

        assertEquals(946685000.0, snapshot.measuredAt, 0.001);
        assertEquals(4321, snapshot.steps);
        assertEquals(68, snapshot.heartRate);
        assertEquals(118, snapshot.systolicBp);
        assertEquals(76, snapshot.diastolicBp);
        assertEquals(99, snapshot.spo2);
        assertEquals(16, snapshot.respirationRate);
        assertEquals(51, snapshot.hrv);
    }

    @Test
    public void ignoresIncompleteComprehensiveRecord() {
        assertNull(RingHealthSnapshot.latestFromPayload(new byte[19], 946684800.0));
    }

    private static void putRecord(
            byte[] target,
            int offset,
            long timestamp,
            int steps,
            int heartRate,
            int systolicBp,
            int diastolicBp,
            int spo2,
            int respirationRate,
            int hrv) {
        target[offset] = (byte) (timestamp & 0xFF);
        target[offset + 1] = (byte) ((timestamp >> 8) & 0xFF);
        target[offset + 2] = (byte) ((timestamp >> 16) & 0xFF);
        target[offset + 3] = (byte) ((timestamp >> 24) & 0xFF);
        target[offset + 4] = (byte) (steps & 0xFF);
        target[offset + 5] = (byte) ((steps >> 8) & 0xFF);
        target[offset + 6] = (byte) heartRate;
        target[offset + 7] = (byte) systolicBp;
        target[offset + 8] = (byte) diastolicBp;
        target[offset + 9] = (byte) spo2;
        target[offset + 10] = (byte) respirationRate;
        target[offset + 11] = (byte) hrv;
    }
}
