package com.aion.chat;

final class PhoneCameraCaptureTiming {
    static final long MIN_SESSION_SETTLE_MS = 700L;

    private PhoneCameraCaptureTiming() {
    }

    static long delayMs(long nowElapsedMs, long targetElapsedMs) {
        if (targetElapsedMs <= 0L) return MIN_SESSION_SETTLE_MS;
        return Math.max(
                MIN_SESSION_SETTLE_MS,
                targetElapsedMs - nowElapsedMs);
    }
}
