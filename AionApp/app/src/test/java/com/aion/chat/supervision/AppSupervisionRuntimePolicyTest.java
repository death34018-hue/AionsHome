package com.aion.chat.supervision;

import org.junit.Test;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class AppSupervisionRuntimePolicyTest {
    @Test
    public void accessibilityEventsStayEventDrivenAndScreenOffIsSilentUntilUserPresent() {
        Harness harness = new Harness(true);
        harness.runtime.onAccessibilityConnected();

        harness.runtime.onPackageForeground("com.example.main");
        assertEquals(0, harness.detector.startCount);

        harness.scheduler.advanceMinutes(5);
        harness.runtime.onScreenOff();
        assertEquals(1, harness.detector.stopCount);
        assertEquals(5 * 60_000L,
                harness.engine.snapshot("group-1", harness.scheduler.elapsedRealtime())
                        .getRoundUsageMs());

        harness.runtime.onAccessibilityUnavailable();
        harness.runtime.onScreenOn();
        assertEquals(0, harness.detector.startCount);

        harness.runtime.onUserPresent();
        assertEquals(1, harness.detector.startCount);
        assertEquals(1, harness.detector.pollNowCount);
        assertTrue(harness.runtime.isScreenReady());
    }

    @Test
    public void coldStartWithoutEnabledAccessibilityStartsFallbackDetection() {
        Harness harness = new Harness(true);

        harness.runtime.onRuntimeStarted(false);

        assertEquals(1, harness.recovery.unavailableCount);
        assertEquals(1, harness.detector.startCount);
    }

    @Test
    public void checkpointEventsAreRecordedInDiagnostics() {
        Harness harness = new Harness(true);
        harness.runtime.onPackageForeground("com.example.main");
        harness.scheduler.advanceMinutes(20);

        harness.runtime.onPackageForeground("com.example.other");

        assertEquals("checkpoint_reached", harness.runtime.logs().get(0).getType());
        assertEquals("group-1", harness.runtime.logs().get(0).getGroupId());
    }

    @Test
    public void delayedSchedulerStillFiresAtTheAbsoluteCheckpointDeadline() {
        Harness harness = new Harness(true);
        RecordingSyncListener listener = new RecordingSyncListener();
        harness.runtime.setSyncListener(listener);

        harness.runtime.onPackageForeground("com.example.main");
        assertEquals("enter", listener.events.get(0));

        harness.scheduler.advanceMinutes(19);
        harness.scheduler.runDue();
        assertFalse(listener.events.contains("checkpoint:group-1:20"));

        harness.scheduler.advanceMinutes(1);
        harness.scheduler.runDue();
        assertTrue(listener.events.contains("checkpoint:group-1:20"));
        assertFalse(listener.events.contains("calibration"));

        int beforeExit = listener.events.size();
        harness.runtime.onPackageForeground("com.example.other");
        assertEquals("exit", listener.events.get(beforeExit));
        harness.scheduler.advanceMinutes(20);
        harness.scheduler.runDue();
        assertEquals(1, java.util.Collections.frequency(
                listener.events, "checkpoint:group-1:20"));
    }

    @Test
    public void screenOffReportsFinalSnapshotThenCancelsCheckpointDeadline() {
        Harness harness = new Harness(true);
        RecordingSyncListener listener = new RecordingSyncListener();
        harness.runtime.setSyncListener(listener);
        harness.runtime.onPackageForeground("com.example.main");

        harness.scheduler.advanceMinutes(5);
        harness.runtime.onScreenOff();
        assertEquals("screen_off", listener.events.get(listener.events.size() - 1));
        int settled = listener.events.size();

        harness.scheduler.advanceMinutes(20);
        harness.scheduler.runDue();
        assertEquals(settled, listener.events.size());
    }

    @Test
    public void heartbeatWatchdogRecoversAStalledDeadlineWithoutDuplicateCheckpoint() {
        Harness harness = new Harness(true);
        RecordingSyncListener listener = new RecordingSyncListener();
        harness.runtime.setSyncListener(listener);
        harness.runtime.onPackageForeground("com.example.main");

        harness.scheduler.advanceMinutes(21);
        harness.runtime.checkpointWatchdog();

        assertEquals(1, java.util.Collections.frequency(
                listener.events, "checkpoint:group-1:20"));

        harness.scheduler.runDue();
        assertEquals(1, java.util.Collections.frequency(
                listener.events, "checkpoint:group-1:20"));
    }

    @Test
    public void userPresentSettlesAnIdleRoundWithoutBackgroundPolling() {
        Harness harness = new Harness(true);
        RecordingSyncListener listener = new RecordingSyncListener();
        harness.runtime.setSyncListener(listener);
        harness.runtime.onPackageForeground("com.example.main");
        harness.scheduler.advanceMinutes(20);
        harness.runtime.onScreenOff();

        harness.scheduler.advanceMinutes(30);
        harness.scheduler.runDue();
        assertEquals(20 * 60_000L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getRoundUsageMs());

        harness.runtime.onUserPresent();

        assertEquals(0L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getRoundUsageMs());
        assertEquals(1, java.util.Collections.frequency(
                listener.events, "round_reset:group-1"));
        assertEquals(0L, harness.store.loadBootScopedState("test-boot")
                .getStates().get("group-1").getRoundUsageMs());
    }

    @Test
    public void statePayloadSettlesExpiredRoundOnlyOnceBeforeExposure() throws Exception {
        Harness harness = new Harness(true);
        RecordingSyncListener listener = new RecordingSyncListener();
        harness.runtime.setSyncListener(listener);
        harness.runtime.onPackageForeground("com.example.main");
        harness.scheduler.advanceMinutes(10);
        harness.runtime.onScreenOff();
        harness.scheduler.advanceMinutes(30);

        org.json.JSONObject first = harness.runtime.buildStatePayload(
                "snapshot", "", 0L);
        org.json.JSONObject second = harness.runtime.buildStatePayload(
                "snapshot", "", 0L);

        assertEquals(0L, first.getJSONArray("groups")
                .getJSONObject(0).getLong("roundUsageMs"));
        assertEquals(0L, second.getJSONArray("groups")
                .getJSONObject(0).getLong("roundUsageMs"));
        assertEquals(1, java.util.Collections.frequency(
                listener.events, "round_reset:group-1"));
    }

    @Test
    public void aiCommandsValidateExpiryFeatureAndDeduplicateWithoutResettingTwice() {
        Harness harness = new Harness(true);
        harness.runtime.onPackageForeground("com.example.main");
        harness.scheduler.advanceMinutes(20);

        AppSupervisionRuntime.CommandResult first = harness.runtime.applyAiCommand(
                "lock", "group-1", 60, "role-main", "休息", "cmd-ai-1",
                harness.scheduler.currentTimeMillis() + 300_000L);
        assertTrue(first.isSuccess());
        assertEquals(60 * 60_000L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getLock().getDurationMs());
        assertEquals(0L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getRoundUsageMs());

        harness.scheduler.advanceMinutes(5);
        AppSupervisionRuntime.CommandResult duplicate = harness.runtime.applyAiCommand(
                "lock", "group-1", 120, "role-main", "again", "cmd-ai-1",
                harness.scheduler.currentTimeMillis() + 300_000L);
        assertTrue(duplicate.isSuccess());
        assertEquals(5 * 60_000L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getRoundUsageMs());

        AppSupervisionRuntime.CommandResult expired = harness.runtime.applyAiCommand(
                "unlock", "group-1", 0, "role-main", "", "cmd-ai-old",
                harness.scheduler.currentTimeMillis() - 1L);
        assertFalse(expired.isSuccess());
        assertEquals("expired", expired.getReason());

        harness.runtime.setFeatureEnabled(false);
        AppSupervisionRuntime.CommandResult disabled = harness.runtime.applyAiCommand(
                "unlock", "group-1", 0, "role-main", "", "cmd-ai-off",
                harness.scheduler.currentTimeMillis() + 300_000L);
        assertFalse(disabled.isSuccess());
        assertEquals("feature_disabled", disabled.getReason());
        assertEquals(EffectiveState.LOCKED, harness.engine.effectiveState(
                "group-1", harness.scheduler.elapsedRealtime()));
    }

    @Test
    public void laterDeviceLockReplacesEarlierAndDeviceUnlockLeavesAppLockUntouched() {
        Harness harness = new Harness(true);
        long future = harness.scheduler.currentTimeMillis() + 300_000L;
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "应用休息", "app-lock");

        assertTrue(harness.runtime.applyAiCommand(
                "device_lock", "", 20, "aion", "先休息", "device-a", future)
                .isSuccess());
        assertTrue(harness.runtime.applyAiCommand(
                "device_lock", "", 45, "connor", "该睡觉了", "device-b", future)
                .isSuccess());
        assertEquals("device-b",
                harness.runtime.deviceSnapshot().getLock().getCommandId());
        assertEquals(45 * 60_000L,
                harness.runtime.deviceSnapshot().getLock().getDurationMs());

        assertTrue(harness.runtime.applyAiCommand(
                "device_unlock", "", 0, "connor", "", "device-c", future)
                .isSuccess());
        assertEquals(EffectiveState.NORMAL,
                harness.runtime.deviceSnapshot().getEffectiveState());
        assertEquals(EffectiveState.LOCKED, harness.engine.effectiveState(
                "group-1", harness.scheduler.elapsedRealtime()));
    }

    @Test
    public void deviceTemporaryUnlockExpiresBackToDeviceLockAndPayloadReportsState()
            throws Exception {
        Harness harness = new Harness(true);
        long future = harness.scheduler.currentTimeMillis() + 300_000L;

        assertTrue(harness.runtime.applyAiCommand(
                "device_lock", "", 60, "aion", "专注", "device-lock", future)
                .isSuccess());
        assertTrue(harness.runtime.applyAiCommand(
                "device_temp_unlock", "", 10, "connor", "临时处理",
                "device-temp", future).isSuccess());
        assertEquals(EffectiveState.TEMPORARILY_UNLOCKED,
                harness.runtime.deviceSnapshot().getEffectiveState());

        org.json.JSONObject payload = harness.runtime.buildStatePayload(
                "snapshot", "", 0L).getJSONObject("deviceLock");
        assertEquals("TEMPORARILY_UNLOCKED", payload.getString("effectiveState"));
        assertEquals("device-lock",
                payload.getJSONObject("lock").getString("commandId"));
        assertEquals("device-temp",
                payload.getJSONObject("temporaryUnlock").getString("commandId"));

        harness.scheduler.advanceMinutes(11);
        assertEquals(EffectiveState.LOCKED,
                harness.runtime.deviceSnapshot().getEffectiveState());
    }

    @Test
    public void deviceCommandsRequireEmptyGroupAndPersistAcrossRuntimeRecreation() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        AppGroup group = AppGroup.create(
                "group-1", "示例应用", Collections.singletonList("com.example.main"), true,
                SupervisionPolicy.of(30 * 60_000L,
                        Collections.singletonList(20 * 60_000L), "role-main"));
        FakeScheduler scheduler = new FakeScheduler();
        AppSupervisionRuntime first = new AppSupervisionRuntime(
                null, new AppSupervisionEngine(true, Collections.singletonList(group)),
                store, new FakeDetector(), new FakeRecovery(), new FakeOverlay(), scheduler);
        long future = scheduler.currentTimeMillis() + 300_000L;

        AppSupervisionRuntime.CommandResult invalid = first.applyAiCommand(
                "device_lock", "group-1", 20, "aion", "休息",
                "device-invalid", future);
        assertFalse(invalid.isSuccess());
        assertEquals("device_group_must_be_empty", invalid.getReason());
        assertTrue(first.applyAiCommand(
                "device_lock", "", 20, "aion", "休息",
                "device-persisted", future).isSuccess());

        AppSupervisionRuntime restored = new AppSupervisionRuntime(
                null, new AppSupervisionEngine(true, Collections.singletonList(group)),
                store, new FakeDetector(), new FakeRecovery(), new FakeOverlay(), scheduler);
        assertEquals("device-persisted",
                restored.deviceSnapshot().getLock().getCommandId());
    }

    @Test
    public void resumedAionsHomeRejectsStaleForeignForegroundEventUntilRealPause() {
        Harness harness = new Harness(true);
        lockDevice(harness, 60, "device-own-app");
        harness.runtime.onPackageForeground("com.example.other");
        assertEquals(SupervisionOverlayDecision.Mode.DEVICE_LOCK,
                harness.overlay.lastMode);

        harness.runtime.onAionsHomeForegroundChanged(true);
        assertEquals(SupervisionOverlayDecision.Mode.HIDDEN,
                harness.overlay.lastMode);
        harness.runtime.onPackageForeground("com.example.other");
        assertEquals(SupervisionOverlayDecision.Mode.HIDDEN,
                harness.overlay.lastMode);

        harness.runtime.onAionsHomeForegroundChanged(false);
        assertEquals(SupervisionOverlayDecision.Mode.HIDDEN,
                harness.overlay.lastMode);
        harness.runtime.onPackageForeground("com.example.other");
        assertEquals(SupervisionOverlayDecision.Mode.DEVICE_LOCK,
                harness.overlay.lastMode);
    }

    @Test
    public void deviceLockWinsThenExpiryRevealsStillActiveAppLock() {
        Harness harness = new Harness(true);
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "应用锁", "app-under-device");
        lockDevice(harness, 10, "device-over-app");

        harness.runtime.onPackageForeground("com.example.main");
        assertEquals(SupervisionOverlayDecision.Mode.DEVICE_LOCK,
                harness.overlay.lastMode);

        harness.scheduler.advanceMinutes(11);
        harness.scheduler.runDue();
        assertEquals(SupervisionOverlayDecision.Mode.APP_LOCK,
                harness.overlay.lastMode);
    }

    @Test
    public void incomingCallSafetyPackageHidesWholeDeviceOverlay() {
        Harness harness = new Harness(true);
        lockDevice(harness, 60, "device-call");
        harness.runtime.onPackageForeground("com.example.other");
        assertEquals(SupervisionOverlayDecision.Mode.DEVICE_LOCK,
                harness.overlay.lastMode);

        harness.runtime.onPackageForeground("com.android.dialer");
        assertEquals(SupervisionOverlayDecision.Mode.HIDDEN,
                harness.overlay.lastMode);
    }

    @Test
    public void emergencyDeviceUnlockLeavesAppLockUntouched() {
        Harness harness = new Harness(true);
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "应用锁", "app-emergency-isolation");
        lockDevice(harness, 60, "device-emergency");

        completeEmergencyUnlock(harness, "__device__");

        assertEquals(EffectiveState.NORMAL,
                harness.runtime.deviceSnapshot().getEffectiveState());
        assertEquals(EffectiveState.LOCKED, harness.engine.effectiveState(
                "group-1", harness.scheduler.elapsedRealtime()));
    }

    @Test
    public void emergencyAppUnlockLeavesDeviceLockUntouched() {
        Harness harness = new Harness(true);
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "应用锁", "app-emergency");
        lockDevice(harness, 60, "device-emergency-isolation");

        completeEmergencyUnlock(harness, "group-1");

        assertEquals(EffectiveState.NORMAL, harness.engine.effectiveState(
                "group-1", harness.scheduler.elapsedRealtime()));
        assertEquals(EffectiveState.LOCKED,
                harness.runtime.deviceSnapshot().getEffectiveState());
    }

    private static void lockDevice(Harness harness, int minutes, String commandId) {
        assertTrue(harness.runtime.applyAiCommand(
                "device_lock", "", minutes, "aion", "专注",
                commandId, harness.scheduler.currentTimeMillis() + 300_000L)
                .isSuccess());
    }

    private static void completeEmergencyUnlock(Harness harness, String targetId) {
        assertEquals(EmergencyUnlockGate.Phase.HOLDING,
                harness.runtime.emergencyBegin(targetId, targetId).getPhase());
        harness.scheduler.advanceMillis(8_000L);
        assertEquals(EmergencyUnlockGate.Phase.REASON_REQUIRED,
                harness.runtime.emergencyHold(targetId, targetId, true).getPhase());
        assertEquals(EmergencyUnlockGate.Phase.WAITING,
                harness.runtime.emergencyReason(
                        targetId, targetId, "需要处理紧急事项").getPhase());
        harness.scheduler.advanceMinutes(1);
        assertTrue(harness.runtime.emergencyConfirm(targetId, targetId).isOk());
    }

    @Test
    public void accessibilityStateTransitionsAreRecordedInDiagnostics() {
        Harness harness = new Harness(true);

        harness.runtime.onAccessibilityUnavailable();
        harness.runtime.onAccessibilityUnavailable();
        harness.runtime.onAccessibilityConnected();
        harness.runtime.onAccessibilityConnected();

        assertEquals(2, harness.runtime.logs().size());
        assertEquals("accessibility_rom_suspended",
                harness.runtime.logs().get(0).getType());
        assertEquals("accessibility_active", harness.runtime.logs().get(1).getType());
    }

    @Test
    public void disabledFeatureIgnoresUsageButStillEvaluatesExistingLockOverlay() {
        Harness harness = new Harness(false);
        harness.engine.setLock(
                "group-1", 20, "role-main", "稍后再用", "cmd-1",
                new SupervisionTime(0L, harness.scheduler.currentTimeMillis()));

        harness.runtime.onPackageForeground("com.example.main");
        harness.scheduler.advanceMinutes(10);
        harness.runtime.onPackageForeground("com.example.other");

        assertEquals(0L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getRoundUsageMs());
        assertTrue(harness.overlay.evaluateCount >= 1);
        assertTrue(harness.overlay.sawLocked);
    }

    @Test
    public void disablingFeatureClosesTheOpenUsageIntervalImmediately() {
        Harness harness = new Harness(true);
        harness.runtime.onPackageForeground("com.example.main");
        harness.scheduler.advanceMinutes(5);

        harness.runtime.setFeatureEnabled(false);
        harness.scheduler.advanceMinutes(5);
        harness.runtime.onScreenOff();

        assertEquals(5 * 60_000L, harness.engine.snapshot(
                "group-1", harness.scheduler.elapsedRealtime()).getRoundUsageMs());
    }

    @Test
    public void disablingFeatureCancelsReportsButKeepsAnExistingLock() {
        Harness harness = new Harness(true);
        RecordingSyncListener listener = new RecordingSyncListener();
        harness.runtime.setSyncListener(listener);
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "稍后再用", "lock-before-off");
        harness.runtime.onPackageForeground("com.example.main");

        harness.runtime.setFeatureEnabled(false);
        int settled = listener.events.size();
        harness.scheduler.advanceMinutes(30);
        harness.scheduler.runDue();

        assertEquals(settled, listener.events.size());
        assertEquals(EffectiveState.LOCKED, harness.engine.effectiveState(
                "group-1", harness.scheduler.elapsedRealtime()));
    }

    @Test
    public void userPresentPerformsOneForegroundCheckAndRestoresTheLockOverlay() {
        Harness harness = new Harness(true);
        harness.detector.oneShotPackage = "com.example.main";
        harness.runtime.onAccessibilityConnected();
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "稍后再用", "lock-screen-cycle");
        harness.runtime.onPackageForeground("com.example.main");
        harness.runtime.onScreenOff();
        harness.overlay.lastState = EffectiveState.NORMAL;

        harness.runtime.onScreenOn();
        harness.runtime.onUserPresent();

        assertEquals(1, harness.detector.oneShotCount);
        assertEquals(EffectiveState.LOCKED, harness.overlay.lastState);
    }

    @Test
    public void theSameVisibleCommandIsReusedInsteadOfRecreated() {
        TimedDirective first = TimedDirective.create(
                100L, 1_000L, 60, "connor", "休息一下", "same-command");
        TimedDirective same = TimedDirective.create(
                200L, 2_000L, 60, "connor", "休息一下", "same-command");
        TimedDirective replacement = TimedDirective.create(
                200L, 2_000L, 60, "connor", "继续休息", "new-command");

        assertTrue(AppSupervisionOverlay.shouldReuseVisibleLock(
                true, "same-command", same));
        assertFalse(AppSupervisionOverlay.shouldReuseVisibleLock(
                true, first.getCommandId(), replacement));
        assertFalse(AppSupervisionOverlay.shouldReuseVisibleLock(
                false, first.getCommandId(), same));
    }

    @Test
    public void aLockedForegroundAppGetsALocalSelfHealCheckThatStopsOnScreenOff() {
        Harness harness = new Harness(true);
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "稍后再用", "lock-self-heal");
        harness.runtime.onPackageForeground("com.example.main");
        int initiallyEvaluated = harness.overlay.evaluateCount;

        harness.scheduler.advanceMillis(2_000L);
        harness.scheduler.runDue();
        assertTrue(harness.overlay.evaluateCount > initiallyEvaluated);

        harness.runtime.onScreenOff();
        int afterScreenOff = harness.overlay.evaluateCount;
        harness.scheduler.advanceMillis(10_000L);
        harness.scheduler.runDue();
        assertEquals(afterScreenOff, harness.overlay.evaluateCount);
    }

    @Test
    public void lockOverlayFormatsCountdownAndConfiguredCommanderDetails() {
        TimedDirective lock = TimedDirective.create(
                100L, 1_800_000_000_000L, 10,
                "role-main", "休息一下", "visual-command");

        assertEquals("09:17", AppSupervisionOverlay.formatRemaining(557_000L));
        assertEquals("01:01:01", AppSupervisionOverlay.formatRemaining(3_661_000L));
        Map<String, String> rows = AppSupervisionOverlay.buildDetailRows(
                lock, "Configured Role");
        assertEquals("Configured Role", rows.get("下令人"));
        assertEquals("10 分钟", rows.get("锁定时长"));
        assertTrue(rows.containsKey("开始时间"));
        assertTrue(rows.containsKey("结束时间"));
        assertFalse(rows.containsKey("下令角色"));
    }

    @Test
    public void lockOverlayUsesWindowScopedImmersiveNavigation() {
        int flags = AppSupervisionOverlay.overlaySystemUiFlags();
        assertTrue((flags & android.view.View.SYSTEM_UI_FLAG_FULLSCREEN) != 0);
        assertTrue((flags & android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION) != 0);
        assertTrue((flags & android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION) != 0);
        assertTrue((flags & android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY) != 0);
    }

    @Test
    public void sameBootRuntimeRecreationRestoresPersistedDirectives() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        AppGroup group = AppGroup.create(
                "group-1", "示例应用", Collections.singletonList("com.example.main"), true,
                SupervisionPolicy.of(30 * 60_000L,
                        Collections.singletonList(20 * 60_000L), "role-main"));
        FakeScheduler scheduler = new FakeScheduler();
        AppSupervisionRuntime first = new AppSupervisionRuntime(
                null,
                new AppSupervisionEngine(true, Collections.singletonList(group)),
                store,
                new FakeDetector(), new FakeRecovery(), new FakeOverlay(), scheduler);
        first.debugSetLock("group-1", 20, "role-main", "稍后再来", "persisted-1");

        AppSupervisionEngine restoredEngine = new AppSupervisionEngine(
                true, Collections.singletonList(group));
        new AppSupervisionRuntime(
                null, restoredEngine, store,
                new FakeDetector(), new FakeRecovery(), new FakeOverlay(), scheduler);

        assertEquals(EffectiveState.LOCKED,
                restoredEngine.effectiveState("group-1", scheduler.elapsedRealtime()));
        assertEquals("persisted-1", restoredEngine.snapshot(
                "group-1", scheduler.elapsedRealtime()).getLock().getCommandId());
    }

    @Test
    public void temporaryUnlockExpiryReevaluatesOverlayWithoutForegroundEvent() {
        Harness harness = new Harness(true);
        harness.runtime.debugSetLock(
                "group-1", 60, "role-main", "稍后再来", "lock-1");
        harness.runtime.debugSetTemporaryUnlock(
                "group-1", 10, "role-main", "临时处理", "unlock-1");
        harness.runtime.onPackageForeground("com.example.main");
        assertEquals(EffectiveState.TEMPORARILY_UNLOCKED, harness.overlay.lastState);

        harness.scheduler.advanceMinutes(11);
        harness.scheduler.runDue();

        assertEquals(EffectiveState.LOCKED, harness.overlay.lastState);
    }

    @Test
    public void runtimeRejectsLockedGroupDeletionAndPersistsSuccessfulDeletion() {
        Harness locked = new Harness(true);
        locked.runtime.debugSetLock(
                "group-1", 20, "role-main", "稍后再用", "lock-delete-guard");

        try {
            locked.runtime.removeGroup("group-1");
            throw new AssertionError("active lock deletion should fail");
        } catch (IllegalStateException expected) {
            assertEquals("active lock must be removed first", expected.getMessage());
        }
        assertTrue(locked.engine.hasGroup("group-1"));

        Harness unlocked = new Harness(true);
        unlocked.runtime.removeGroup("group-1");

        assertFalse(unlocked.engine.hasGroup("group-1"));
        assertTrue(unlocked.store.loadConfig().getGroups().isEmpty());
        assertTrue(unlocked.store.loadBootScopedState("test-boot").getStates().isEmpty());
    }

    private static final class Harness {
        final AppSupervisionEngine engine;
        final FakeDetector detector = new FakeDetector();
        final FakeRecovery recovery = new FakeRecovery();
        final FakeOverlay overlay = new FakeOverlay();
        final FakeScheduler scheduler = new FakeScheduler();
        final AppSupervisionStore store = new AppSupervisionStore(new MemoryBackend());
        final AppSupervisionRuntime runtime;

        Harness(boolean featureEnabled) {
            AppGroup group = AppGroup.create(
                    "group-1",
                    "示例应用",
                    Collections.singletonList("com.example.main"),
                    true,
                    SupervisionPolicy.of(
                            30 * 60_000L,
                            Collections.singletonList(20 * 60_000L),
                            "role-main"));
            engine = new AppSupervisionEngine(
                    featureEnabled, Collections.singletonList(group));
            runtime = new AppSupervisionRuntime(
                    null, engine, store, detector, recovery, overlay, scheduler);
        }
    }

    private static final class FakeDetector extends ForegroundAppDetector {
        int startCount;
        int stopCount;
        int pollNowCount;
        int oneShotCount;
        String oneShotPackage;

        @Override public void start(Listener listener) { startCount++; }
        @Override public void stop() { stopCount++; }
        @Override public void pollNow() { pollNowCount++; }
        @Override public void pollOnce(Listener listener) {
            oneShotCount++;
            if (oneShotPackage != null) listener.onForegroundPackage(oneShotPackage);
        }
    }

    private static final class FakeRecovery extends AccessibilityRecoveryController {
        int connectedCount;
        int unavailableCount;

        FakeRecovery() { super(); }

        @Override public void onAccessibilityConnected() { connectedCount++; }
        @Override public void onAccessibilityUnavailable(boolean screenReady) {
            unavailableCount++;
        }
    }

    private static final class FakeOverlay extends AppSupervisionOverlay {
        int evaluateCount;
        EffectiveState lastState = EffectiveState.NORMAL;
        boolean sawLocked;
        SupervisionOverlayDecision.Mode lastMode =
                SupervisionOverlayDecision.Mode.HIDDEN;

        FakeOverlay() { super(); }

        @Override
        public void evaluate(String packageName, EffectiveState state,
                AppGroup group, AppGroupState.Snapshot snapshot, SupervisionTime now) {
            evaluateCount++;
            lastState = state;
            sawLocked |= state == EffectiveState.LOCKED;
            lastMode = state == EffectiveState.LOCKED
                    ? SupervisionOverlayDecision.Mode.APP_LOCK
                    : SupervisionOverlayDecision.Mode.HIDDEN;
        }

        @Override public void evaluateDevice(
                TimedDirective directive, SupervisionTime now) {
            evaluateCount++;
            lastMode = SupervisionOverlayDecision.Mode.DEVICE_LOCK;
        }

        @Override public void hide() {
            lastMode = SupervisionOverlayDecision.Mode.HIDDEN;
        }
    }

    private static final class FakeScheduler implements AppSupervisionRuntime.Scheduler {
        private long elapsedMs;
        private long wallMs = 1_800_000_000_000L;
        private final java.util.List<Scheduled> scheduled = new java.util.ArrayList<>();

        @Override public long elapsedRealtime() { return elapsedMs; }
        @Override public long currentTimeMillis() { return wallMs; }
        @Override public void schedule(Runnable runnable, long delayMs) {
            scheduled.add(new Scheduled(elapsedMs + delayMs, runnable));
        }
        @Override public void cancelAll() { scheduled.clear(); }

        void advanceMinutes(int minutes) {
            long delta = minutes * 60_000L;
            advanceMillis(delta);
        }

        void advanceMillis(long delta) {
            elapsedMs += delta;
            wallMs += delta;
        }

        void runDue() {
            java.util.List<Scheduled> due = new java.util.ArrayList<>();
            for (Scheduled item : scheduled) if (item.atMs <= elapsedMs) due.add(item);
            scheduled.removeAll(due);
            for (Scheduled item : due) item.runnable.run();
        }

        private static final class Scheduled {
            final long atMs;
            final Runnable runnable;
            Scheduled(long atMs, Runnable runnable) {
                this.atMs = atMs;
                this.runnable = runnable;
            }
        }
    }

    private static final class RecordingSyncListener
            implements AppSupervisionRuntime.SyncListener {
        final java.util.List<String> events = new java.util.ArrayList<>();

        @Override
        public void onStateEvent(String eventType, String triggerGroupId,
                long checkpointMs) {
            if ("checkpoint".equals(eventType)) {
                events.add("checkpoint:" + triggerGroupId + ":" + (checkpointMs / 60_000L));
            } else if ("round_reset".equals(eventType)) {
                events.add("round_reset:" + triggerGroupId);
            } else {
                events.add(eventType);
            }
        }
    }

    private static final class MemoryBackend implements AppSupervisionStore.Backend {
        private final Map<String, String> values = new LinkedHashMap<>();
        @Override public String getString(String key, String defaultValue) {
            String value = values.get(key);
            return value == null ? defaultValue : value;
        }
        @Override public void putString(String key, String value) {
            values.put(key, value);
        }
    }
}
