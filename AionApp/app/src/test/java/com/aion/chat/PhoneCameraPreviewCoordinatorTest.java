package com.aion.chat;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PhoneCameraPreviewCoordinatorTest {
    @Test
    public void eventWithoutVisibleClientNeedsNoWait() {
        PhoneCameraPreviewCoordinator coordinator =
                new PhoneCameraPreviewCoordinator();

        assertTrue(coordinator.pauseForEvent(20L));
        assertTrue(coordinator.isEventActive());

        coordinator.finishEvent();
        assertFalse(coordinator.isEventActive());
    }

    @Test
    public void registeredClientReleasesThenResumesAroundEvent() {
        PhoneCameraPreviewCoordinator coordinator =
                new PhoneCameraPreviewCoordinator();
        RecordingClient client = new RecordingClient(true);
        coordinator.register(client);

        assertTrue(coordinator.pauseForEvent(20L));
        assertTrue(coordinator.isEventActive());
        assertTrue(client.paused);
        assertFalse(client.resumed);

        coordinator.finishEvent();
        assertFalse(coordinator.isEventActive());
        assertTrue(client.resumed);
    }

    @Test
    public void timeoutIsReportedButFinishStillResumesClient() {
        PhoneCameraPreviewCoordinator coordinator =
                new PhoneCameraPreviewCoordinator();
        RecordingClient client = new RecordingClient(false);
        coordinator.register(client);

        assertFalse(coordinator.pauseForEvent(5L));
        assertTrue(coordinator.isEventActive());

        coordinator.finishEvent();
        assertTrue(client.resumed);
    }

    @Test
    public void unregisteredActivityDoesNotReceiveResume() {
        PhoneCameraPreviewCoordinator coordinator =
                new PhoneCameraPreviewCoordinator();
        RecordingClient client = new RecordingClient(true);
        coordinator.register(client);
        assertTrue(coordinator.pauseForEvent(20L));

        coordinator.unregister(client);
        coordinator.finishEvent();

        assertFalse(client.resumed);
    }

    @Test
    public void repeatedFinishDoesNotResumeTwice() {
        PhoneCameraPreviewCoordinator coordinator =
                new PhoneCameraPreviewCoordinator();
        RecordingClient client = new RecordingClient(true);
        coordinator.register(client);
        assertTrue(coordinator.pauseForEvent(20L));

        coordinator.finishEvent();
        coordinator.finishEvent();

        assertTrue(client.resumed);
        assertTrue(client.resumeCount == 1);
    }

    private static final class RecordingClient
            implements PhoneCameraPreviewCoordinator.Client {
        private final boolean acknowledgePause;
        boolean paused;
        boolean resumed;
        int resumeCount;

        RecordingClient(boolean acknowledgePause) {
            this.acknowledgePause = acknowledgePause;
        }

        @Override
        public void pauseForEvent(Runnable released) {
            paused = true;
            if (acknowledgePause) released.run();
        }

        @Override
        public void resumeAfterEvent() {
            resumed = true;
            resumeCount++;
        }
    }
}
