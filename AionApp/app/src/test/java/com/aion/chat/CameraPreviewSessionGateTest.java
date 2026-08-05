package com.aion.chat;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class CameraPreviewSessionGateTest {
    @Test
    public void stopAndRestartInvalidateOldFrameAndErrorCallbacks() {
        CameraPreviewSessionGate gate = new CameraPreviewSessionGate();
        long first = gate.start();
        assertTrue(gate.isCurrent(first));

        gate.stop();
        long second = gate.start();

        assertNotEquals(first, second);
        assertFalse(gate.isCurrent(first));
        assertTrue(gate.isCurrent(second));
    }
}
