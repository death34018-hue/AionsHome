package com.aion.chat.miband;

import java.io.ByteArrayOutputStream;
import java.security.GeneralSecurityException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Calendar;
import java.util.List;
import java.util.zip.CRC32;

import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;

public final class MiBandProtocol {
    public static final int AUTH_ENDPOINT = 0x0082;
    public static final int CONFIG_ENDPOINT = 0x000A;
    public static final int HEART_RATE_ENDPOINT = 0x001D;
    public static final int FIND_DEVICE_ENDPOINT = 0x001A;
    public static final int NOTIFICATION_ENDPOINT = 0x001E;

    private static final int MAX_NOTE_CODEPOINTS = 80;
    private static final int MAX_SENDER_CODEPOINTS = 30;

    public static final String CHUNKED_WRITE_UUID = "00000016-0000-3512-2118-0009af100700";
    public static final String CHUNKED_READ_UUID = "00000017-0000-3512-2118-0009af100700";
    public static final String FETCH_METADATA_UUID = "00000004-0000-3512-2118-0009af100700";
    public static final String FETCH_DATA_UUID = "00000005-0000-3512-2118-0009af100700";
    public static final String HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb";
    public static final String BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb";

    private MiBandProtocol() {}

    public static byte[] buildNotification(int id, String senderName, String note) {
        String sender = normalizeText(senderName, MAX_SENDER_CODEPOINTS);
        if (sender.isEmpty()) sender = "AI";
        String body = normalizeText(note, MAX_NOTE_CODEPOINTS);
        if (body.isEmpty()) throw new IllegalArgumentException("notification note is required");

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(0x03); // send notification
        writeU32(out, id & 0xffffffffL);
        out.write(0xfa); // normal notification
        out.write(0x00); // show
        writeZeroTerminated(out, "com.aion.chat");
        writeZeroTerminated(out, ""); // title: combined body below is firmware-independent
        writeZeroTerminated(out, sender + "：" + body);
        writeZeroTerminated(out, "AionsHome");
        out.write(0x00); // no reply action
        return out.toByteArray();
    }

    private static void writeZeroTerminated(ByteArrayOutputStream out, String value) {
        byte[] bytes = (value == null ? "" : value).getBytes(StandardCharsets.UTF_8);
        out.write(bytes, 0, bytes.length);
        out.write(0x00);
    }

    private static String normalizeText(String value, int maxCodePoints) {
        String normalized = (value == null ? "" : value).replaceAll("\\s+", " ").trim();
        int count = normalized.codePointCount(0, normalized.length());
        if (count <= maxCodePoints) return normalized;
        return normalized.substring(0, normalized.offsetByCodePoints(0, maxCodePoints));
    }

    public static int parseHeartRate(byte[] payload) {
        if (payload == null || payload.length == 0) {
            throw new IllegalArgumentException("invalid heart rate payload");
        }
        boolean sixteenBit = (payload[0] & 0x01) != 0;
        int required = sixteenBit ? 3 : 2;
        if (payload.length < required) {
            throw new IllegalArgumentException("invalid heart rate payload");
        }
        return sixteenBit
                ? unsigned(payload[1]) | (unsigned(payload[2]) << 8)
                : unsigned(payload[1]);
    }

    public static byte[] buildActivityFetch(Calendar since) {
        if (since == null) throw new IllegalArgumentException("since is required");
        int year = since.get(Calendar.YEAR);
        int quarterHours = since.getTimeZone().getOffset(since.getTimeInMillis()) / (15 * 60_000);
        return new byte[] {
                0x01,
                0x01,
                (byte) (year & 0xff),
                (byte) ((year >>> 8) & 0xff),
                (byte) (since.get(Calendar.MONTH) + 1),
                (byte) since.get(Calendar.DAY_OF_MONTH),
                (byte) since.get(Calendar.HOUR_OF_DAY),
                (byte) since.get(Calendar.MINUTE),
                (byte) since.get(Calendar.SECOND),
                (byte) quarterHours,
        };
    }

    public static List<ActivitySample> parseActivity(long startMillis, byte[] payload) {
        if (payload == null || payload.length % 8 != 0) {
            throw new IllegalArgumentException("activity payload length must be divisible by 8");
        }
        List<ActivitySample> out = new ArrayList<>();
        for (int offset = 0; offset < payload.length; offset += 8) {
            int rawKind = unsigned(payload[offset]);
            int deepSleep = unsigned(payload[offset + 6]);
            int remSleep = unsigned(payload[offset + 7]);
            String sleepStage = null;
            if (rawKind == 120) {
                if ((remSleep & 0x7f) > 55) sleepStage = "rem";
                else if ((deepSleep & 0x7f) > 42) sleepStage = "deep";
                else sleepStage = "light";
            }
            out.add(new ActivitySample(
                    startMillis + (offset / 8L) * 60_000L,
                    rawKind,
                    unsigned(payload[offset + 1]),
                    unsigned(payload[offset + 2]),
                    unsigned(payload[offset + 3]),
                    unsigned(payload[offset + 4]),
                    unsigned(payload[offset + 5]),
                    deepSleep,
                    remSleep,
                    sleepStage));
        }
        return out;
    }

    public static final class ActivitySample {
        public final long timestampMillis;
        public final int rawKind;
        public final int intensity;
        public final int steps;
        public final int heartRate;
        public final int unknown;
        public final int sleep;
        public final int deepSleep;
        public final int remSleep;
        public final String sleepStage;

        ActivitySample(long timestampMillis, int rawKind, int intensity, int steps,
                       int heartRate, int unknown, int sleep, int deepSleep,
                       int remSleep, String sleepStage) {
            this.timestampMillis = timestampMillis;
            this.rawKind = rawKind;
            this.intensity = intensity;
            this.steps = steps;
            this.heartRate = heartRate;
            this.unknown = unknown;
            this.sleep = sleep;
            this.deepSleep = deepSleep;
            this.remSleep = remSleep;
            this.sleepStage = sleepStage;
        }
    }

    public static final class DecodedMessage {
        public final int endpoint;
        public final byte[] payload;
        public final boolean needsAck;
        public final byte[] ack;

        DecodedMessage(int endpoint, byte[] payload, boolean needsAck, byte[] ack) {
            this.endpoint = endpoint;
            this.payload = payload;
            this.needsAck = needsAck;
            this.ack = ack;
        }
    }

    public static final class ChunkEncoder {
        private final int mtu;
        private long encryptedSequence;
        private byte[] sessionKey;

        public ChunkEncoder(int mtu) {
            if (mtu < 23) throw new IllegalArgumentException("MTU must be at least 23");
            this.mtu = mtu;
        }

        public void setEncryptionParameters(long sequence, byte[] key) {
            requireKey(key);
            encryptedSequence = sequence & 0xffffffffL;
            sessionKey = Arrays.copyOf(key, key.length);
        }

        public List<byte[]> encode(int endpoint, byte[] payload, int handle, boolean encrypt)
                throws GeneralSecurityException {
            if (endpoint < 0 || endpoint > 0xffff) throw new IllegalArgumentException("endpoint");
            if (handle < 0 || handle > 0xff) throw new IllegalArgumentException("handle");
            byte[] source = payload == null ? new byte[0] : payload;
            int originalLength = source.length;
            byte[] encoded = source;
            if (encrypt) {
                if (sessionKey == null) throw new IllegalStateException("encryption is not configured");
                ByteArrayOutputStream authenticated = new ByteArrayOutputStream();
                authenticated.write(source, 0, source.length);
                writeU32(authenticated, encryptedSequence);
                encryptedSequence = (encryptedSequence + 1) & 0xffffffffL;
                byte[] authenticatedBytes = authenticated.toByteArray();
                CRC32 crc = new CRC32();
                crc.update(authenticatedBytes);
                ByteArrayOutputStream plain = new ByteArrayOutputStream();
                plain.write(authenticatedBytes, 0, authenticatedBytes.length);
                writeU32(plain, crc.getValue());
                int padded = ((plain.size() + 15) / 16) * 16;
                byte[] paddedBytes = Arrays.copyOf(plain.toByteArray(), padded);
                encoded = aes(true, messageKey(sessionKey, handle), paddedBytes);
            }

            List<byte[]> frames = new ArrayList<>();
            int offset = 0;
            int count = 0;
            do {
                boolean first = count == 0;
                int headerSize = first ? 11 : 5;
                int capacity = mtu - 3 - headerSize;
                int partLength = Math.min(capacity, encoded.length - offset);
                boolean last = offset + partLength >= encoded.length;
                int flags = (first ? 0x01 : 0) | (encrypt ? 0x08 : 0) | (last ? 0x06 : 0);
                ByteArrayOutputStream frame = new ByteArrayOutputStream();
                frame.write(0x03);
                frame.write(flags);
                frame.write(0x00);
                frame.write(handle);
                frame.write(count);
                if (first) {
                    writeU32(frame, originalLength);
                    frame.write(endpoint & 0xff);
                    frame.write((endpoint >>> 8) & 0xff);
                }
                if (partLength > 0) frame.write(encoded, offset, partLength);
                frames.add(frame.toByteArray());
                offset += partLength;
                count++;
            } while (offset < encoded.length);
            return frames;
        }
    }

    public static final class ChunkDecoder {
        private Integer handle;
        private int lastCount = -1;
        private int originalLength;
        private int endpoint;
        private boolean encrypted;
        private final ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        private byte[] sessionKey;

        public void setEncryptionParameters(byte[] key) {
            requireKey(key);
            sessionKey = Arrays.copyOf(key, key.length);
        }

        public DecodedMessage feed(byte[] frame) throws GeneralSecurityException {
            if (frame == null || frame.length < 5 || frame[0] != 0x03) {
                throw new IllegalArgumentException("invalid chunk frame");
            }
            int flags = unsigned(frame[1]);
            int frameHandle = unsigned(frame[3]);
            int count = unsigned(frame[4]);
            boolean first = (flags & 0x01) != 0;
            boolean frameEncrypted = (flags & 0x08) != 0;
            int payloadOffset;
            if (first) {
                if (frame.length < 11 || count != 0) throw new IllegalArgumentException("invalid first chunk");
                handle = frameHandle;
                lastCount = 0;
                originalLength = readU32(frame, 5);
                endpoint = unsigned(frame[9]) | (unsigned(frame[10]) << 8);
                encrypted = frameEncrypted;
                buffer.reset();
                payloadOffset = 11;
            } else {
                if (handle == null || handle != frameHandle || count != lastCount + 1) {
                    throw new IllegalArgumentException("unexpected chunk sequence");
                }
                if (encrypted != frameEncrypted) throw new IllegalArgumentException("encryption flag changed");
                lastCount = count;
                payloadOffset = 5;
            }
            buffer.write(frame, payloadOffset, frame.length - payloadOffset);
            if ((flags & 0x02) == 0) return null;

            int transferLength = encrypted ? ((originalLength + 8 + 15) / 16) * 16 : originalLength;
            byte[] collected = buffer.toByteArray();
            if (collected.length < transferLength) throw new IllegalArgumentException("truncated chunk payload");
            byte[] payload;
            if (encrypted) {
                if (sessionKey == null) throw new IllegalStateException("encrypted chunk without session key");
                byte[] plain = aes(false, messageKey(sessionKey, frameHandle),
                        Arrays.copyOf(collected, transferLength));
                CRC32 crc = new CRC32();
                crc.update(plain, 0, originalLength + 4);
                long expected = readU32Long(plain, originalLength + 4);
                if (crc.getValue() != expected) throw new IllegalArgumentException("encrypted chunk checksum mismatch");
                payload = Arrays.copyOf(plain, originalLength);
            } else {
                payload = Arrays.copyOf(collected, originalLength);
            }
            byte[] ack = new byte[] {0x04, 0x00, (byte) frameHandle, 0x01, (byte) count};
            DecodedMessage result = new DecodedMessage(endpoint, payload, (flags & 0x04) != 0, ack);
            handle = null;
            buffer.reset();
            encrypted = false;
            return result;
        }
    }

    private static byte[] messageKey(byte[] key, int handle) {
        byte[] out = new byte[key.length];
        for (int i = 0; i < key.length; i++) out[i] = (byte) (key[i] ^ handle);
        return out;
    }

    private static byte[] aes(boolean encrypt, byte[] key, byte[] payload)
            throws GeneralSecurityException {
        Cipher cipher = Cipher.getInstance("AES/ECB/NoPadding");
        cipher.init(encrypt ? Cipher.ENCRYPT_MODE : Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"));
        return cipher.doFinal(payload);
    }

    private static void requireKey(byte[] key) {
        if (key == null || key.length != 16) throw new IllegalArgumentException("session key must be 16 bytes");
    }

    private static void writeU32(ByteArrayOutputStream out, long value) {
        out.write((int) value & 0xff);
        out.write((int) (value >>> 8) & 0xff);
        out.write((int) (value >>> 16) & 0xff);
        out.write((int) (value >>> 24) & 0xff);
    }

    private static int readU32(byte[] data, int offset) {
        return (int) readU32Long(data, offset);
    }

    private static long readU32Long(byte[] data, int offset) {
        return (long) unsigned(data[offset])
                | ((long) unsigned(data[offset + 1]) << 8)
                | ((long) unsigned(data[offset + 2]) << 16)
                | ((long) unsigned(data[offset + 3]) << 24);
    }

    private static int unsigned(byte value) {
        return value & 0xff;
    }
}
