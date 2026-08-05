package com.aion.chat;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PhoneCameraCaptureGateTest {
    @Test
    public void cancelInvalidatesLateCallbacksBeforeNextCapture() {
        PhoneCameraCaptureGate gate = new PhoneCameraCaptureGate();
        long first = gate.begin();
        assertTrue(gate.isCurrent(first));

        gate.cancel();
        long second = gate.begin();

        assertNotEquals(first, second);
        assertFalse(gate.isCurrent(first));
        assertTrue(gate.isCurrent(second));
        assertFalse(gate.complete(first));
        assertTrue(gate.complete(second));
    }

    @Test
    public void concurrentBeginIsRejectedUntilCurrentCaptureCompletes() {
        PhoneCameraCaptureGate gate = new PhoneCameraCaptureGate();
        long first = gate.begin();

        assertTrue(first > 0L);
        assertTrue(gate.begin() < 0L);
        assertTrue(gate.complete(first));
        assertTrue(gate.begin() > 0L);
    }
}
