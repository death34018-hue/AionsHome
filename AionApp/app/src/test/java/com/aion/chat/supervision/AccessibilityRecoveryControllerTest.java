package com.aion.chat.supervision;

import org.junit.Test;

import java.util.Arrays;
import java.util.LinkedHashSet;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class AccessibilityRecoveryControllerTest {
    @Test
    public void disconnectAlwaysEntersSuspendedButSensitiveForegroundDoesNotWrite() {
        Harness harness = new Harness();

        harness.controller.onAccessibilityUnavailable(false);
        assertEquals(RecoveryState.ROM_SUSPENDED, harness.controller.getState());
        harness.controller.onUserPresent();
        harness.controller.onForegroundPackage("com.eg.android.AlipayGphone", false);

        assertEquals(0, harness.writer.writeCount);
        assertEquals(RecoveryState.ROM_SUSPENDED, harness.controller.getState());

        harness.controller.onAccessibilityUnavailable(true);
        assertEquals(RecoveryState.ROM_SUSPENDED, harness.controller.getState());
    }

    @Test
    public void monitoredForegroundRequestsRestoreAndUsesOneTwoFiveSecondRetries() {
        Harness harness = new Harness();
        harness.controller.onAccessibilityUnavailable(true);

        harness.controller.onForegroundPackage("com.example.monitored", true);

        assertEquals(1, harness.writer.writeCount);
        assertEquals(RecoveryState.RECOVERING, harness.controller.getState());
        assertEquals(1_000L, harness.scheduler.lastDelayMs);
        harness.scheduler.runScheduled();
        assertEquals(2, harness.writer.writeCount);
        assertEquals(2_000L, harness.scheduler.lastDelayMs);
        harness.scheduler.runScheduled();
        assertEquals(3, harness.writer.writeCount);
        assertEquals(5_000L, harness.scheduler.lastDelayMs);
        assertEquals(RecoveryState.RECOVERING, harness.controller.getState());
    }

    @Test
    public void screenOffCancelsRetriesAndConnectedCallbackAloneMarksActive() {
        Harness harness = new Harness();
        harness.controller.onAccessibilityUnavailable(true);
        harness.controller.onForegroundPackage("com.example.monitored", true);
        assertTrue(harness.scheduler.hasScheduled());

        harness.controller.onScreenOff();
        assertFalse(harness.scheduler.hasScheduled());
        assertEquals(RecoveryState.ROM_SUSPENDED, harness.controller.getState());

        harness.controller.onAccessibilityConnected();
        assertEquals(RecoveryState.ACTIVE, harness.controller.getState());
        assertFalse(harness.scheduler.hasScheduled());
    }

    @Test
    public void missingSecureSettingsPermissionIsTerminalAndVisible() {
        Harness harness = new Harness();
        harness.writer.hasPermission = false;
        harness.controller.onAccessibilityUnavailable(true);

        harness.controller.onForegroundPackage("com.example.monitored", true);

        assertEquals(RecoveryState.PERMISSION_MISSING, harness.controller.getState());
        assertEquals(0, harness.writer.writeCount);
        assertFalse(harness.scheduler.hasScheduled());
        assertTrue(harness.controller.getDiagnostic().contains("WRITE_SECURE_SETTINGS"));
    }

    private static final class Harness {
        final FakeWriter writer = new FakeWriter();
        final FakeRetryScheduler scheduler = new FakeRetryScheduler();
        final AccessibilityRecoveryController controller =
                new AccessibilityRecoveryController(
                        writer,
                        scheduler,
                        "com.aion.chat/.AionAccessibilityService",
                        new LinkedHashSet<>(Arrays.asList(
                                "com.eg.android.AlipayGphone",
                                "com.tencent.mm")));
    }

    private static final class FakeWriter
            implements AccessibilityRecoveryController.SecureSettingsWriter {
        boolean hasPermission = true;
        int writeCount;

        @Override public boolean hasPermission() { return hasPermission; }
        @Override public boolean enableServicePreservingOthers(String componentName) {
            writeCount++;
            return true;
        }
    }

    private static final class FakeRetryScheduler
            implements AccessibilityRecoveryController.RetryScheduler {
        Runnable scheduled;
        long lastDelayMs;
        int cancelCount;

        @Override public void schedule(Runnable runnable, long delayMs) {
            scheduled = runnable;
            lastDelayMs = delayMs;
        }

        @Override public void cancel() {
            scheduled = null;
            cancelCount++;
        }

        boolean hasScheduled() { return scheduled != null; }

        void runScheduled() {
            Runnable runnable = scheduled;
            scheduled = null;
            runnable.run();
        }
    }
}
