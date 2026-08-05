package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class CameraPreviewStateTest {
    @Test
    public void cameraErrorInvalidatesRunningPreviewAndOldFrame() {
        CameraPreviewState state = new CameraPreviewState();
        state.started();
        state.receivedFrame(12_345L);

        assertTrue(state.isRunning());
        assertEquals(12_345L, state.getLastFrameAt());

        state.failed();

        assertFalse(state.isRunning());
        assertEquals(0L, state.getLastFrameAt());
    }

    @Test
    public void stopInvalidatesRunningPreviewAndOldFrame() {
        CameraPreviewState state = new CameraPreviewState();
        state.started();
        state.receivedFrame(99L);

        state.stopped();

        assertFalse(state.isRunning());
        assertEquals(0L, state.getLastFrameAt());
    }

    @Test
    public void lateFrameAfterStopCannotMakePreviewLookFresh() {
        CameraPreviewState state = new CameraPreviewState();
        state.started();
        state.stopped();

        state.receivedFrame(88_000L);

        assertFalse(state.isRunning());
        assertEquals(0L, state.getLastFrameAt());
    }

    @Test
    public void backgroundActivityCannotStartNativePreview() {
        CameraPreviewState state = new CameraPreviewState();

        assertFalse(state.canStart());
        state.setAppForeground(false);
        assertFalse(state.canStart());

        state.setAppForeground(true);
        assertTrue(state.canStart());
    }
}
