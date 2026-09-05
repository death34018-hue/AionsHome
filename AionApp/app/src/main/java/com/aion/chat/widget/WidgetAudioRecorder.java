package com.aion.chat.widget;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;

final class WidgetAudioRecorder {
    private static final int SAMPLE_RATE = 16000;
    private static final int CHANNEL = AudioFormat.CHANNEL_IN_MONO;
    private static final int ENCODING = AudioFormat.ENCODING_PCM_16BIT;
    private static final long MIN_DURATION_MS = 300L;

    private AudioRecord recorder;
    private Thread captureThread;
    private ByteArrayOutputStream pcm;
    private volatile boolean recording;
    private long startedAt;

    boolean start() {
        if (recording) return false;
        int minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING);
        int bufferSize = Math.max(minBuffer, 4096);
        recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, CHANNEL, ENCODING, bufferSize);
        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            recorder.release();
            recorder = null;
            return false;
        }

        pcm = new ByteArrayOutputStream();
        recorder.startRecording();
        recording = true;
        startedAt = System.currentTimeMillis();
        captureThread = new Thread(() -> capture(bufferSize), "WidgetMemoRecorder");
        captureThread.start();
        return true;
    }

    boolean isRecording() {
        return recording;
    }

    File stopToWav(File output) throws IOException {
        long duration = System.currentTimeMillis() - startedAt;
        byte[] data = stopAndTakePcm();
        if (duration < MIN_DURATION_MS || data.length < SAMPLE_RATE / 2) {
            if (output.exists()) output.delete();
            return null;
        }
        writeWav(output, data);
        return output;
    }

    void cancel() {
        stopAndTakePcm();
    }

    private void capture(int bufferSize) {
        byte[] buffer = new byte[bufferSize];
        while (recording) {
            AudioRecord active = recorder;
            if (active == null) break;
            int read = active.read(buffer, 0, buffer.length);
            if (read > 0) {
                synchronized (this) {
                    if (pcm != null) pcm.write(buffer, 0, read);
                }
            }
        }
    }

    private byte[] stopAndTakePcm() {
        recording = false;
        AudioRecord active = recorder;
        recorder = null;
        if (active != null) {
            try { active.stop(); } catch (RuntimeException ignored) {}
        }
        Thread thread = captureThread;
        captureThread = null;
        if (thread != null) {
            try { thread.join(800L); } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        if (active != null) recorderRelease(active);
        synchronized (this) {
            byte[] result = pcm == null ? new byte[0] : pcm.toByteArray();
            pcm = null;
            return result;
        }
    }

    private void recorderRelease(AudioRecord recorder) {
        recorder.release();
    }

    private static void writeWav(File output, byte[] pcmData) throws IOException {
        try (FileOutputStream stream = new FileOutputStream(output)) {
            int dataLength = pcmData.length;
            int byteRate = SAMPLE_RATE * 2;
            stream.write(new byte[]{'R', 'I', 'F', 'F'});
            writeLittleEndian(stream, 36 + dataLength, 4);
            stream.write(new byte[]{'W', 'A', 'V', 'E', 'f', 'm', 't', ' '});
            writeLittleEndian(stream, 16, 4);
            writeLittleEndian(stream, 1, 2);
            writeLittleEndian(stream, 1, 2);
            writeLittleEndian(stream, SAMPLE_RATE, 4);
            writeLittleEndian(stream, byteRate, 4);
            writeLittleEndian(stream, 2, 2);
            writeLittleEndian(stream, 16, 2);
            stream.write(new byte[]{'d', 'a', 't', 'a'});
            writeLittleEndian(stream, dataLength, 4);
            stream.write(pcmData);
        }
    }

    private static void writeLittleEndian(FileOutputStream stream, int value, int bytes)
            throws IOException {
        for (int i = 0; i < bytes; i++) stream.write((value >> (8 * i)) & 0xff);
    }
}
