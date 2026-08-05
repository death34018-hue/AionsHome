package com.aion.chat;

final class PhoneCameraRetryPolicy {
    private static final int MAX_ATTEMPTS = 6;
    private static final long DEADLINE_SAFETY_MS = 1_000L;
    private static final long MIN_CAPTURE_ATTEMPT_MS = 500L;
    private static final long MAX_CAPTURE_TIMEOUT_MS = 15_000L;
    private static final long[] DELAYS_MS = {250L, 500L, 1_000L, 2_000L, 3_000L};

    private PhoneCameraRetryPolicy() {
    }

    static boolean shouldRetry(
            String error,
            int attempt,
            long nowMs,
            long deadlineMs
    ) {
        if (attempt >= MAX_ATTEMPTS || !isTemporary(error)) return false;
        return nowMs + delayMs(attempt) + DEADLINE_SAFETY_MS
                + MIN_CAPTURE_ATTEMPT_MS < deadlineMs;
    }

    static long delayMs(int attempt) {
        int index = Math.max(0, Math.min(attempt - 1, DELAYS_MS.length - 1));
        return DELAYS_MS[index];
    }

    static long captureTimeoutMs(long nowMs, long deadlineMs) {
        long remaining = deadlineMs - nowMs - DEADLINE_SAFETY_MS;
        if (remaining < MIN_CAPTURE_ATTEMPT_MS) return 0L;
        return Math.min(MAX_CAPTURE_TIMEOUT_MS, remaining);
    }

    private static boolean isTemporary(String error) {
        if (error == null) return false;
        return "camera_busy".equals(error)
                || "preview_release_timeout".equals(error)
                || "camera_error_1".equals(error)
                || "camera_error_2".equals(error)
                || "camera_error_4".equals(error)
                || "camera_error_5".equals(error)
                || "camera_disconnected".equals(error)
                || "camera_access_in_use".equals(error)
                || "camera_access_max_in_use".equals(error)
                || "camera_access_disconnected".equals(error)
                || "camera_access_error".equals(error)
                || "capture_session_access_failed".equals(error);
    }
}
