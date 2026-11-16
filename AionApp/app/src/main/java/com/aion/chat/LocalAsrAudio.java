package com.aion.chat;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

final class LocalAsrAudio {
    /** The canonical 16 kHz mono PCM WAV emitted by the app's microphone bridge. */
    static byte[] pcmFromWav(byte[] wav) {
        if (wav.length < 46 || wav.length > 44 + 16000 * 2 * 120)
            throw new IllegalArgumentException("本地转写需要 16 kHz 单声道 WAV");
        ByteBuffer b = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        if (!new String(wav, 0, 4, StandardCharsets.US_ASCII).equals("RIFF")
                || !new String(wav, 8, 8, StandardCharsets.US_ASCII).equals("WAVEfmt ")
                || b.getInt(16) != 16 || b.getShort(20) != 1 || b.getShort(22) != 1
                || b.getInt(24) != 16000 || b.getShort(34) != 16
                || !new String(wav, 36, 4, StandardCharsets.US_ASCII).equals("data")
                || b.getInt(40) != wav.length - 44 || (wav.length - 44) % 2 != 0)
            throw new IllegalArgumentException("本地转写需要 16 kHz 单声道 WAV");
        return Arrays.copyOfRange(wav, 44, wav.length);
    }

    static float[] samples(byte[] pcm) {
        float[] result = new float[pcm.length / 2];
        for (int i = 0; i < result.length; i++) {
            result[i] = (short) ((pcm[2*i] & 255) | (pcm[2*i+1] << 8)) / 32768f;
        }
        return result;
    }

    static byte[] wav(byte[] pcm) {
        ByteBuffer b = ByteBuffer.allocate(44 + pcm.length).order(ByteOrder.LITTLE_ENDIAN);
        b.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + pcm.length);
        b.put("WAVEfmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16);
        b.putShort((short) 1).putShort((short) 1).putInt(16000).putInt(32000);
        b.putShort((short) 2).putShort((short) 16);
        b.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(pcm.length).put(pcm);
        return b.array();
    }
}
