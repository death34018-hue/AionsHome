package com.aion.chat.miband;

import org.junit.Test;

import java.util.Calendar;
import java.util.List;
import java.util.TimeZone;
import java.nio.charset.StandardCharsets;

import static org.junit.Assert.*;

public class MiBandProtocolTest {
    @Test
    public void negotiatedLargeMtuKeepsAuthenticationCommandInOneFrame() throws Exception {
        MiBandProtocol.ChunkEncoder encoder = new MiBandProtocol.ChunkEncoder(247);
        assertEquals(1, encoder.encode(MiBandProtocol.AUTH_ENDPOINT, new byte[52], 1, false).size());
    }

    @Test
    public void chunkEncoderAndDecoderRoundTripAcrossMultipleFrames() throws Exception {
        byte[] payload = new byte[52];
        for (int i = 0; i < payload.length; i++) payload[i] = (byte) i;

        MiBandProtocol.ChunkEncoder encoder = new MiBandProtocol.ChunkEncoder(23);
        List<byte[]> frames = encoder.encode(0x0082, payload, 7, false);
        MiBandProtocol.ChunkDecoder decoder = new MiBandProtocol.ChunkDecoder();
        MiBandProtocol.DecodedMessage result = null;
        for (byte[] frame : frames) {
            MiBandProtocol.DecodedMessage next = decoder.feed(frame);
            if (next != null) result = next;
        }

        assertTrue(frames.size() > 1);
        assertArrayEquals(new byte[] {0x03, 0x01, 0x00, 0x07, 0x00}, firstFive(frames.get(0)));
        assertNotNull(result);
        assertEquals(0x0082, result.endpoint);
        assertArrayEquals(payload, result.payload);
        assertTrue(result.needsAck);
        assertArrayEquals(
                new byte[] {0x04, 0x00, 0x07, 0x01, (byte) (frames.size() - 1)},
                result.ack);
    }

    @Test
    public void encryptedChunksRoundTripWithCrcAndPadding() throws Exception {
        byte[] key = hex("00112233445566778899aabbccddeeff");
        byte[] payload = hex("030008050111121331");
        MiBandProtocol.ChunkEncoder encoder = new MiBandProtocol.ChunkEncoder(23);
        encoder.setEncryptionParameters(0x12345678L, key);
        MiBandProtocol.ChunkDecoder decoder = new MiBandProtocol.ChunkDecoder();
        decoder.setEncryptionParameters(key);

        MiBandProtocol.DecodedMessage result = null;
        List<byte[]> frames = encoder.encode(0x000A, payload, 3, true);
        for (byte[] frame : frames) {
            MiBandProtocol.DecodedMessage next = decoder.feed(frame);
            if (next != null) result = next;
        }

        assertTrue((frames.get(0)[1] & 0x08) != 0);
        assertNotNull(result);
        assertEquals(0x000A, result.endpoint);
        assertArrayEquals(payload, result.payload);
    }

    @Test
    public void parsesEightAndSixteenBitHeartRate() {
        assertEquals(72, MiBandProtocol.parseHeartRate(new byte[] {0x00, 72}));
        assertEquals(300, MiBandProtocol.parseHeartRate(new byte[] {0x01, 0x2c, 0x01}));
    }

    @Test(expected = IllegalArgumentException.class)
    public void rejectsTruncatedHeartRate() {
        MiBandProtocol.parseHeartRate(new byte[] {0x01, 0x20});
    }

    @Test
    public void activityParserEmitsMinuteSamplesAndRawFields() {
        long start = 1_752_710_400_000L;
        byte[] payload = new byte[] {
                120, 8, 12, 71, 0, 2, 50, 0,
                118, 4, 3, 72, 0, 0, (byte) 128, 1
        };

        List<MiBandProtocol.ActivitySample> samples = MiBandProtocol.parseActivity(start, payload);

        assertEquals(2, samples.size());
        assertEquals(start, samples.get(0).timestampMillis);
        assertEquals(start + 60_000L, samples.get(1).timestampMillis);
        assertEquals(12, samples.get(0).steps);
        assertEquals(71, samples.get(0).heartRate);
        assertEquals("deep", samples.get(0).sleepStage);
        assertNull(samples.get(1).sleepStage);
    }

    @Test
    public void activityFetchUsesZeppTimestampAndQuarterHourOffset() {
        Calendar since = Calendar.getInstance(TimeZone.getTimeZone("GMT+08:00"));
        since.clear();
        since.set(2026, Calendar.JULY, 17, 14, 3, 2);

        assertArrayEquals(
                hex("0101ea0707110e030220"),
                MiBandProtocol.buildActivityFetch(since));
    }

    @Test
    public void notificationPayloadContainsConfiguredSenderAndNoteAsUtf8Body() {
        byte[] payload = MiBandProtocol.buildNotification(0x12345678, "星澜", "滚起来活动！");

        assertEquals(0x03, payload[0] & 0xff);
        assertArrayEquals(hex("78563412"), slice(payload, 1, 5));
        assertEquals(0xfa, payload[5] & 0xff);
        assertEquals(0x00, payload[6] & 0xff);

        int bodyStart = findZero(payload, 7) + 1;
        bodyStart = findZero(payload, bodyStart) + 1;
        int bodyEnd = findZero(payload, bodyStart);
        assertEquals(
                "星澜：滚起来活动！",
                new String(payload, bodyStart, bodyEnd - bodyStart, StandardCharsets.UTF_8));
        assertEquals(0, payload[payload.length - 1]);
    }

    @Test
    public void notificationPayloadSafelyTruncatesUnicodeCodePoints() {
        StringBuilder note = new StringBuilder();
        for (int i = 0; i < 81; i++) note.append("🦴");
        byte[] payload = MiBandProtocol.buildNotification(7, "星澜", note.toString());
        String body = nullTerminatedField(payload, 2, 7);

        assertEquals(83, body.codePointCount(0, body.length()));
        assertTrue(body.startsWith("星澜："));
        assertFalse(body.endsWith("?"));
    }

    private static byte[] firstFive(byte[] value) {
        byte[] out = new byte[5];
        System.arraycopy(value, 0, out, 0, out.length);
        return out;
    }

    private static byte[] slice(byte[] value, int start, int end) {
        byte[] out = new byte[end - start];
        System.arraycopy(value, start, out, 0, out.length);
        return out;
    }

    private static int findZero(byte[] value, int start) {
        for (int i = start; i < value.length; i++) if (value[i] == 0) return i;
        throw new AssertionError("missing NUL terminator");
    }

    private static String nullTerminatedField(byte[] value, int skipFields, int start) {
        int fieldStart = start;
        for (int i = 0; i < skipFields; i++) fieldStart = findZero(value, fieldStart) + 1;
        int fieldEnd = findZero(value, fieldStart);
        return new String(value, fieldStart, fieldEnd - fieldStart, StandardCharsets.UTF_8);
    }

    static byte[] hex(String value) {
        byte[] out = new byte[value.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }
}
