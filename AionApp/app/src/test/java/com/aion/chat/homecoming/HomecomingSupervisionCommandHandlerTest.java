package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionCommandHandlerTest {
    @Test
    public void lockUsesDeterministicIdAndExistingLocalRuntime() {
        RecordingRuntime runtime = new RecordingRuntime(true, "", "短视频");
        HomecomingSupervisionCommandHandler handler =
                new HomecomingSupervisionCommandHandler(runtime, "epoch-one");
        HomecomingControlParser.ControlEvent event =
                event("[APP_LOCK:group-video|35|先休息一下]");

        HomecomingSupervisionCommandHandler.ApplyResult first = handler.apply(
                "request-one", 2, event, "second", 1_000L);
        HomecomingSupervisionCommandHandler.ApplyResult duplicate = handler.apply(
                "request-one", 2, event, "second", 1_001L);

        assertTrue(first.applied);
        assertEquals("lock", first.action);
        assertEquals(35, first.minutes);
        assertEquals("短视频", first.groupDisplayName);
        assertEquals(runtime.commandIds.get(0), runtime.commandIds.get(1));
        assertEquals("connor", runtime.roleIds.get(0));
        assertEquals(301_000L, runtime.expiresAt.get(0).longValue());
        assertEquals(first.commandId, duplicate.commandId);
    }

    @Test
    public void unlockAndTemporaryUnlockKeepExistingProtocolShapes() {
        RecordingRuntime runtime = new RecordingRuntime(true, "", "阅读");
        HomecomingSupervisionCommandHandler handler =
                new HomecomingSupervisionCommandHandler(runtime, "epoch-one");

        HomecomingSupervisionCommandHandler.ApplyResult temporary = handler.apply(
                "request-temp", 0,
                event("[APP_TEMP_UNLOCK:group-reading|10|查资料]"),
                "main", 2_000L);
        HomecomingSupervisionCommandHandler.ApplyResult unlock = handler.apply(
                "request-unlock", 0,
                event("[APP_UNLOCK:group-reading]"),
                "main", 3_000L);

        assertTrue(temporary.applied);
        assertEquals("temp_unlock", temporary.action);
        assertEquals(10, temporary.minutes);
        assertTrue(unlock.applied);
        assertEquals("unlock", unlock.action);
        assertEquals(0, unlock.minutes);
        assertEquals("aion", runtime.roleIds.get(0));
    }

    @Test
    public void malformedOrUnsupportedCommandNeverReachesRuntime() {
        RecordingRuntime runtime = new RecordingRuntime(true, "", "阅读");
        HomecomingSupervisionCommandHandler handler =
                new HomecomingSupervisionCommandHandler(runtime, "epoch-one");

        HomecomingSupervisionCommandHandler.ApplyResult badMinutes = handler.apply(
                "request-one", 0,
                event("[APP_LOCK:group-reading|not-a-number|休息]"),
                "main", 1_000L);
        HomecomingSupervisionCommandHandler.ApplyResult schedule = handler.apply(
                "request-two", 0,
                event("[ALARM:2030-01-02T08:00|起床]"),
                "main", 1_000L);

        assertFalse(badMinutes.applied);
        assertEquals("invalid", badMinutes.status);
        assertEquals("deferred", schedule.status);
        assertTrue(runtime.commandIds.isEmpty());
    }

    @Test
    public void localRuntimeRejectionIsReturnedWithoutFalseSuccess() {
        RecordingRuntime runtime =
                new RecordingRuntime(false, "feature_disabled", "短视频");
        HomecomingSupervisionCommandHandler handler =
                new HomecomingSupervisionCommandHandler(runtime, "epoch-one");

        HomecomingSupervisionCommandHandler.ApplyResult result = handler.apply(
                "request-one", 0,
                event("[APP_LOCK:group-video|20|先休息]"),
                "second", 1_000L);

        assertFalse(result.applied);
        assertEquals("feature_disabled", result.status);
    }

    private static HomecomingControlParser.ControlEvent event(String tag) {
        return HomecomingControlParser.parse(tag).events.get(0);
    }

    private static final class RecordingRuntime
            implements HomecomingSupervisionCommandHandler.RuntimePort {
        final boolean success;
        final String reason;
        final String displayName;
        final List<String> commandIds = new ArrayList<>();
        final List<String> roleIds = new ArrayList<>();
        final List<Long> expiresAt = new ArrayList<>();

        RecordingRuntime(boolean success, String reason, String displayName) {
            this.success = success;
            this.reason = reason;
            this.displayName = displayName;
        }

        @Override
        public HomecomingSupervisionCommandHandler.RuntimeResult apply(
                String action,
                String groupId,
                int minutes,
                String roleId,
                String message,
                String commandId,
                long expiresWallMs) {
            commandIds.add(commandId);
            roleIds.add(roleId);
            expiresAt.add(expiresWallMs);
            return new HomecomingSupervisionCommandHandler.RuntimeResult(
                    success, reason, displayName);
        }
    }
}
