package com.aion.chat;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class PhoneCameraCaptureTimingTest {
    @Test
    public void waitsForFutureAudioMarkerButNeverLessThanSettleTime() {
        assertEquals(4_500L, PhoneCameraCaptureTiming.delayMs(500L, 5_000L));
        assertEquals(700L, PhoneCameraCaptureTiming.delayMs(4_600L, 5_000L));
        assertEquals(700L, PhoneCameraCaptureTiming.delayMs(5_200L, 5_000L));
        assertEquals(700L, PhoneCameraCaptureTiming.delayMs(1_000L, 0L));
    }
}
