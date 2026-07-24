package com.aion.chat;

import org.json.JSONException;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

final class RingHealthSnapshot {
    private static final int RECORD_LENGTH = 20;

    final double measuredAt;
    final int steps;
    final int heartRate;
    final int systolicBp;
    final int diastolicBp;
    final int spo2;
    final int respirationRate;
    final int hrv;

    private RingHealthSnapshot(
            double measuredAt,
            int steps,
            int heartRate,
            int systolicBp,
            int diastolicBp,
            int spo2,
            int respirationRate,
            int hrv) {
        this.measuredAt = measuredAt;
        this.steps = steps;
        this.heartRate = heartRate;
        this.systolicBp = systolicBp;
        this.diastolicBp = diastolicBp;
        this.spo2 = spo2;
        this.respirationRate = respirationRate;
        this.hrv = hrv;
    }

    static RingHealthSnapshot latestFromPayload(byte[] payload, double epoch2000Seconds) {
        if (payload == null || payload.length < RECORD_LENGTH) return null;
        RingHealthSnapshot latest = null;
        for (int offset = 0; offset + RECORD_LENGTH <= payload.length; offset += RECORD_LENGTH) {
            long timestamp = readU32LE(payload, offset);
            RingHealthSnapshot candidate = new RingHealthSnapshot(
                    epoch2000Seconds + timestamp,
                    readU16LE(payload, offset + 4),
                    payload[offset + 6] & 0xFF,
                    payload[offset + 7] & 0xFF,
                    payload[offset + 8] & 0xFF,
                    payload[offset + 9] & 0xFF,
                    payload[offset + 10] & 0xFF,
                    payload[offset + 11] & 0xFF
            );
            if (latest == null || candidate.measuredAt > latest.measuredAt) latest = candidate;
        }
        return latest;
    }

    JSONObject toJson() throws JSONException {
        JSONObject json = new JSONObject();
        json.put("time", isoTimestamp(measuredAt));
        json.put("steps", steps);
        json.put("hr", heartRate);
        json.put("sbp", systolicBp);
        json.put("dbp", diastolicBp);
        json.put("spo2", spo2);
        json.put("resp", respirationRate);
        json.put("hrv", hrv);
        return json;
    }

    private static int readU16LE(byte[] data, int offset) {
        return (data[offset] & 0xFF) | ((data[offset + 1] & 0xFF) << 8);
    }

    private static long readU32LE(byte[] data, int offset) {
        return ((long) data[offset] & 0xFF)
                | (((long) data[offset + 1] & 0xFF) << 8)
                | (((long) data[offset + 2] & 0xFF) << 16)
                | (((long) data[offset + 3] & 0xFF) << 24);
    }

    private static String isoTimestamp(double epochSeconds) {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US);
        format.setTimeZone(TimeZone.getTimeZone("UTC"));
        return format.format(new Date(Math.round(epochSeconds * 1000.0)));
    }
}
