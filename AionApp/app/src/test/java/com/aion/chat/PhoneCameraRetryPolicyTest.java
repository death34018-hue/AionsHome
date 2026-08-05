package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PhoneCameraRetryPolicyTest {
    @Test
    public void retriesTemporaryCameraOwnershipFailuresBeforeDeadline() {
        long now = 10_000L;
        long deadline = 30_000L;

        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "camera_error_1", 1, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "camera_error_2", 2, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "camera_disconnected", 3, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "camera_access_in_use", 4, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "camera_access_max_in_use", 4, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "capture_session_access_failed", 5, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "camera_busy", 1, now, deadline));
        assertTrue(PhoneCameraRetryPolicy.shouldRetry(
                "preview_release_timeout", 1, now, deadline));
    }

    @Test
    public void doesNotRetryPermanentFailuresOrExhaustedRequest() {
        long now = 10_000L;
        long deadline = 30_000L;

        assertFalse(PhoneCameraRetryPolicy.shouldRetry(
                "camera_permission_denied", 1, now, deadline));
        assertFalse(PhoneCameraRetryPolicy.shouldRetry(
                "camera_not_found", 1, now, deadline));
        assertFalse(PhoneCameraRetryPolicy.shouldRetry(
                "camera_access_disabled", 1, now, deadline));
        assertFalse(PhoneCameraRetryPolicy.shouldRetry(
                "image_processing_failed:IllegalStateException", 1, now, deadline));
        assertFalse(PhoneCameraRetryPolicy.shouldRetry(
                "camera_error_1", 6, now, deadline));
        assertFalse(PhoneCameraRetryPolicy.shouldRetry(
                "camera_error_1", 1, 28_900L, deadline));
    }

    @Test
    public void retryDelayUsesShortCappedBackoff() {
        assertEquals(250L, PhoneCameraRetryPolicy.delayMs(1));
        assertEquals(500L, PhoneCameraRetryPolicy.delayMs(2));
        assertEquals(1_000L, PhoneCameraRetryPolicy.delayMs(3));
        assertEquals(2_000L, PhoneCameraRetryPolicy.delayMs(4));
        assertEquals(3_000L, PhoneCameraRetryPolicy.delayMs(5));
        assertEquals(3_000L, PhoneCameraRetryPolicy.delayMs(20));
    }

    @Test
    public void captureTimeoutNeverCrossesRequestDeadline() {
        assertEquals(15_000L,
                PhoneCameraRetryPolicy.captureTimeoutMs(10_000L, 30_000L));
        assertEquals(1_000L,
                PhoneCameraRetryPolicy.captureTimeoutMs(10_000L, 12_000L));
        assertEquals(0L,
                PhoneCameraRetryPolicy.captureTimeoutMs(10_000L, 10_900L));
        assertEquals(0L,
                PhoneCameraRetryPolicy.captureTimeoutMs(10_000L, 11_499L));
        assertEquals(500L,
                PhoneCameraRetryPolicy.captureTimeoutMs(10_000L, 11_500L));
    }
}
