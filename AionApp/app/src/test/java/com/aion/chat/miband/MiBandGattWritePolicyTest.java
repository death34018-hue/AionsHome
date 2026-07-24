package com.aion.chat.miband;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class MiBandGattWritePolicyTest {
    @Test
    public void retriesAndroidBusyStatusOnlyWithinBound() {
        assertTrue(MiBandGattWritePolicy.shouldRetry(201, 0));
        assertTrue(MiBandGattWritePolicy.shouldRetry(201, 5));
        assertFalse(MiBandGattWritePolicy.shouldRetry(201, 6));
        assertFalse(MiBandGattWritePolicy.shouldRetry(7, 0));
    }

    @Test
    public void busyRetryDelayIsShortAndBounded() {
        assertEquals(80L, MiBandGattWritePolicy.retryDelayMillis(0));
        assertEquals(130L, MiBandGattWritePolicy.retryDelayMillis(1));
        assertEquals(250L, MiBandGattWritePolicy.retryDelayMillis(100));
    }

    @Test
    public void prefersAcknowledgedWriteWhenCharacteristicSupportsIt() {
        assertEquals(2, MiBandGattWritePolicy.writeType(8 | 4));
        assertEquals(1, MiBandGattWritePolicy.writeType(4));
    }
}
