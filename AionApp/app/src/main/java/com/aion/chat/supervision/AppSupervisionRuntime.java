package com.aion.chat.supervision;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

public final class AppSupervisionRuntime {
    public static final String DEVICE_EMERGENCY_TARGET_ID = "__device__";
    private static final Set<String> PROTECTED_PACKAGES = new HashSet<>(Arrays.asList(
            "com.aion.chat", "com.android.systemui", "com.android.settings",
            "com.android.phone", "com.android.dialer", "com.google.android.dialer",
            "com.android.server.telecom", "com.android.incallui",
            "com.google.android.incallui"));
    private static final long LOCK_GUARD_INTERVAL_MS = 2_000L;
    public interface Scheduler {
        long elapsedRealtime();
        long currentTimeMillis();
        void schedule(Runnable runnable, long delayMs);
        void cancelAll();
    }

    public interface SyncListener {
        void onStateEvent(String eventType, String triggerGroupId, long checkpointMs);
    }

    public static final class CommandResult {
        private final boolean success;
        private final String reason;

        CommandResult(boolean success, String reason) {
            this.success = success;
            this.reason = reason == null ? "" : reason;
        }

        public boolean isSuccess() { return success; }
        public String getReason() { return reason; }
    }

    private static AppSupervisionRuntime instance;

    private final Context context;
    private final AppSupervisionEngine engine;
    private final DeviceLockState deviceLockState = new DeviceLockState();
    private final AppSupervisionStore store;
    private final ForegroundAppDetector detector;
    private final AccessibilityRecoveryController recovery;
    private final AppSupervisionOverlay overlay;
    private final Scheduler scheduler;
    private final CheckpointDeadlineScheduler checkpointDeadlineScheduler;
    private final String bootId;
    private final EmergencyUnlockGate emergencyGate;

    private boolean accessibilityActive;
    private boolean screenReady = true;
    private boolean detectorPolling;
    private boolean persistenceScheduled;
    private String currentForegroundPackage = "";
    private String scheduledTemporaryUnlockCommandId = "";
    private String scheduledDeviceStateKey = "";
    private RecoveryState lastLoggedRecoveryState;
    private SyncListener syncListener;
    private String activeLockGuardKey = "";
    private boolean aionsHomeActivityResumed;
    private boolean aionsHomeForeground;
    private long foregroundGeneration;

    AppSupervisionRuntime(Context context, AppSupervisionEngine engine,
            AppSupervisionStore store, ForegroundAppDetector detector,
            AccessibilityRecoveryController recovery, AppSupervisionOverlay overlay,
            Scheduler scheduler) {
        if (engine == null || store == null || detector == null || recovery == null
                || overlay == null || scheduler == null) {
            throw new IllegalArgumentException("runtime collaborators are required");
        }
        this.context = context;
        this.engine = engine;
        this.store = store;
        this.detector = detector;
        this.recovery = recovery;
        this.overlay = overlay;
        this.scheduler = scheduler;
        this.checkpointDeadlineScheduler = context == null
                ? CheckpointDeadlineScheduler.forTests(scheduler)
                : CheckpointDeadlineScheduler.production();
        this.bootId = context == null ? "test-boot" : readBootId(context);
        this.emergencyGate = new EmergencyUnlockGate(
                new EmergencyUnlockGate.LockController() {
                    @Override public boolean isLocked(String groupId) {
                        if (DEVICE_EMERGENCY_TARGET_ID.equals(groupId)) {
                            return AppSupervisionRuntime.this.deviceLockState.effectiveState(
                                    AppSupervisionRuntime.this.scheduler.elapsedRealtime())
                                    == EffectiveState.LOCKED;
                        }
                        return AppSupervisionRuntime.this.engine.effectiveState(
                                groupId, AppSupervisionRuntime.this.scheduler.elapsedRealtime())
                                == EffectiveState.LOCKED;
                    }

                    @Override public void removeLock(String groupId) {
                        SupervisionTime now = AppSupervisionRuntime.this.now();
                        if (DEVICE_EMERGENCY_TARGET_ID.equals(groupId)) {
                            AppSupervisionRuntime.this.deviceLockState.removeLock();
                            AppGroup foreground =
                                    AppSupervisionRuntime.this.engine.groupForPackage(
                                            AppSupervisionRuntime.this
                                                    .currentForegroundPackage);
                            AppSupervisionRuntime.this.evaluateOverlay(
                                    AppSupervisionRuntime.this.currentForegroundPackage,
                                    foreground,
                                    now);
                        } else {
                            AppSupervisionRuntime.this.engine.removeLock(
                                    groupId,
                                    "emergency-" + now.getElapsedMs(),
                                    now);
                        }
                        AppSupervisionRuntime.this.persistRuntime();
                    }
                },
                (event, groupId, reason) -> AppSupervisionRuntime.this.store.appendLog(
                        new AppSupervisionStore.LogRecord(
                                AppSupervisionRuntime.this.scheduler.currentTimeMillis(),
                                "emergency_unlock_" + event,
                                groupId,
                                reason)));
        restoreRuntime();
    }

    public static synchronized AppSupervisionRuntime start(Context context) {
        if (instance != null) {
            return instance;
        }
        Context appContext = context.getApplicationContext();
        AppSupervisionStore store = new AppSupervisionStore(appContext);
        AppSupervisionStore.ConfigSnapshot config = store.loadConfig();
        AppSupervisionEngine engine = new AppSupervisionEngine(
                config.isFeatureEnabled(), config.getGroups());
        instance = new AppSupervisionRuntime(
                appContext,
                engine,
                store,
                ForegroundAppDetector.create(appContext),
                new AccessibilityRecoveryController(appContext),
                new AppSupervisionOverlay(appContext),
                new MainScheduler());
        instance.onRuntimeStarted(isAccessibilityServiceEnabled(appContext));
        return instance;
    }

    public static synchronized AppSupervisionRuntime get() {
        return instance;
    }

    public static synchronized void stop() {
        if (instance != null) {
            instance.shutdown();
            instance = null;
        }
    }

    public void onAccessibilityConnected() {
        accessibilityActive = true;
        stopDetectorIfRunning();
        recovery.onAccessibilityConnected();
        appendRecoveryDiagnostic(
                RecoveryState.ACTIVE,
                "accessibility_active",
                "accessibility service connected");
    }

    void onRuntimeStarted(boolean accessibilityEnabled) {
        if (!accessibilityEnabled) {
            onAccessibilityUnavailable();
        }
    }

    public void onAccessibilityUnavailable() {
        accessibilityActive = false;
        detector.cancelPendingOneShot();
        recovery.onAccessibilityUnavailable(screenReady);
        appendRecoveryDiagnostic(
                RecoveryState.ROM_SUSPENDED,
                "accessibility_rom_suspended",
                "accessibility service suspended by system");
        if (screenReady) {
            startDetector();
        }
    }

    public synchronized void onPackageForeground(String packageName) {
        if (packageName == null || packageName.trim().isEmpty()) {
            return;
        }
        String ownPackage = context == null ? "com.aion.chat" : context.getPackageName();
        if (packageName.equals(ownPackage)) {
            aionsHomeForeground = true;
        } else if (aionsHomeActivityResumed) {
            return;
        } else {
            aionsHomeForeground = false;
        }
        foregroundGeneration++;
        SupervisionTime now = now();
        AppGroup previousGroup = engine.groupForPackage(currentForegroundPackage);
        if (!packageName.equals(currentForegroundPackage)) {
            scheduledTemporaryUnlockCommandId = "";
        }
        currentForegroundPackage = packageName;
        List<EngineEvent> events = engine.onForegroundChanged(
                packageName, now.getElapsedMs(), now.getWallMs());
        AppGroup group = engine.groupForPackage(packageName);
        String previousGroupId = monitoredGroupId(previousGroup);
        String nextGroupId = monitoredGroupId(group);
        if (!sameGroup(previousGroupId, nextGroupId)) {
            checkpointDeadlineScheduler.cancel();
            if (previousGroupId != null) notifySync("exit", previousGroupId, 0L);
            if (nextGroupId != null) {
                notifySync("enter", nextGroupId, 0L);
                scheduleCheckpointDeadline(nextGroupId);
            }
        }
        recovery.onForegroundPackage(packageName, group != null && group.isMonitored());
        if (!events.isEmpty()) {
            recordEngineEvents(events);
            persistRuntime();
        } else if (group != null && engine.isFeatureEnabled()) {
            scheduleUsagePersistence();
        }
        evaluateOverlay(packageName, group, now);
    }

    public synchronized void onAionsHomeForegroundChanged(boolean resumed) {
        aionsHomeActivityResumed = resumed;
        foregroundGeneration++;
        if (resumed) {
            aionsHomeForeground = true;
            onPackageForeground(
                    context == null ? "com.aion.chat" : context.getPackageName());
        }
    }

    public synchronized void onScreenOff() {
        screenReady = false;
        AppGroup closingGroup = engine.groupForPackage(currentForegroundPackage);
        String closingGroupId = monitoredGroupId(closingGroup);
        currentForegroundPackage = "";
        checkpointDeadlineScheduler.cancel();
        activeLockGuardKey = "";
        scheduledTemporaryUnlockCommandId = "";
        scheduledDeviceStateKey = "";
        aionsHomeActivityResumed = false;
        aionsHomeForeground = false;
        foregroundGeneration++;
        SupervisionTime now = now();
        List<EngineEvent> events = engine.onScreenOff(now.getElapsedMs(), now.getWallMs());
        detector.stop();
        detectorPolling = false;
        scheduler.cancelAll();
        persistenceScheduled = false;
        recovery.onScreenOff();
        overlay.onScreenOff();
        if (!events.isEmpty()) {
            recordEngineEvents(events);
            persistRuntime();
        }
        if (closingGroupId != null) notifySync("screen_off", closingGroupId, 0L);
    }

    public synchronized void onScreenOn() {
        screenReady = false;
    }

    public synchronized void onUserPresent() {
        screenReady = true;
        recovery.onUserPresent();
        settleIdleRounds();
        engine.onUserPresent(scheduler.elapsedRealtime(), scheduler.currentTimeMillis());
        if (!accessibilityActive) {
            startDetector();
            detector.pollNow();
        } else {
            reconcileForegroundOnce();
        }
    }

    public synchronized void reconcileForegroundOnce() {
        if (!screenReady) return;
        long generation = foregroundGeneration;
        detector.pollOnce(packageName -> {
            synchronized (AppSupervisionRuntime.this) {
                if (generation != foregroundGeneration) return;
            }
            onPackageForeground(packageName);
        });
    }

    public boolean isScreenReady() {
        return screenReady;
    }

    public AppSupervisionEngine engine() {
        return engine;
    }

    public synchronized void setSyncListener(SyncListener listener) {
        this.syncListener = listener;
    }

    public synchronized JSONObject buildStatePayload(
            String eventType, String triggerGroupId, long checkpointMs) throws Exception {
        settleIdleRounds();
        long nowElapsed = scheduler.elapsedRealtime();
        long nowWall = scheduler.currentTimeMillis();
        JSONObject out = new JSONObject();
        out.put("eventId", "app-" + nowWall + "-" + eventType + "-"
                + (triggerGroupId == null ? "" : triggerGroupId) + "-" + checkpointMs);
        out.put("eventType", eventType == null ? "snapshot" : eventType);
        out.put("triggerGroupId", triggerGroupId == null ? "" : triggerGroupId);
        out.put("checkpointMinutes", checkpointMs <= 0 ? 0 : checkpointMs / 60_000L);
        JSONArray groups = new JSONArray();
        for (AppGroup group : engine.groups()) {
            AppGroupState.Snapshot state = engine.snapshot(group.getGroupId(), nowElapsed);
            JSONObject item = new JSONObject();
            item.put("groupId", group.getGroupId());
            item.put("displayName", group.getDisplayName());
            item.put("roleId", group.getPolicy().getRoleId());
            item.put("roundUsageMs", state.getRoundUsageMs());
            item.put("foregroundOpen", state.isForegroundOpen());
            item.put("effectiveState", engine.effectiveState(
                    group.getGroupId(), nowElapsed).name());
            item.put("lock", directiveJson(state.getLock()));
            item.put("temporaryUnlock", directiveJson(state.getTemporaryUnlock()));
            groups.put(item);
        }
        out.put("groups", groups);
        DeviceLockState.Snapshot device = deviceSnapshot();
        JSONObject deviceJson = new JSONObject();
        deviceJson.put("effectiveState", device.getEffectiveState().name());
        deviceJson.put("lock", directiveJson(device.getLock()));
        deviceJson.put("temporaryUnlock", directiveJson(device.getTemporaryUnlock()));
        out.put("deviceLock", deviceJson);
        return out;
    }

    private static Object directiveJson(TimedDirective directive) throws Exception {
        if (directive == null) return JSONObject.NULL;
        JSONObject value = new JSONObject();
        value.put("deadlineWallMs", directive.getDeadlineWallMs());
        value.put("durationMs", directive.getDurationMs());
        value.put("roleId", directive.getRoleId());
        value.put("message", directive.getMessage());
        value.put("commandId", directive.getCommandId());
        return value;
    }

    public EmergencyUnlockGate emergencyGate() {
        return emergencyGate;
    }

    public synchronized DeviceLockState.Snapshot deviceSnapshot() {
        return deviceLockState.snapshot(scheduler.elapsedRealtime());
    }

    public synchronized void upsertGroup(AppGroup group) {
        for (String packageName : group.getPackageNames()) {
            if (isProtectedPackage(packageName)) {
                throw new IllegalArgumentException("protected package: " + packageName);
            }
        }
        engine.upsertGroup(group);
        persistConfig();
        rescheduleCheckpointDeadline();
        if (engine.isFeatureEnabled()) notifySync("config", group.getGroupId(), 0L);
    }

    public synchronized void removeGroup(String groupId) {
        engine.removeGroup(groupId, scheduler.elapsedRealtime());
        persistConfig();
        persistRuntime();
        rescheduleCheckpointDeadline();
        if (engine.isFeatureEnabled()) notifySync("config", groupId, 0L);
    }

    public synchronized void setFeatureEnabled(boolean enabled) {
        if (!enabled && engine.isFeatureEnabled()) {
            SupervisionTime now = now();
            engine.onForegroundChanged("", now.getElapsedMs(), now.getWallMs());
        }
        engine.setFeatureEnabled(enabled);
        persistConfig();
        persistRuntime();
        rescheduleCheckpointDeadline();
        if (enabled) notifySync("config", "", 0L);
    }

    public synchronized void clearRound(String groupId) {
        engine.clearRound(groupId);
        persistRuntime();
        rescheduleCheckpointDeadline();
        if (engine.isFeatureEnabled()) notifySync("clear", groupId, 0L);
    }

    public synchronized void settleIdleRounds() {
        SupervisionTime now = now();
        List<EngineEvent> events = engine.expireIdleRounds(
                now.getElapsedMs(), now.getWallMs());
        if (events.isEmpty()) return;
        recordEngineEvents(events);
        persistRuntime();
    }

    public synchronized void checkpointWatchdog() {
        if (!screenReady || !engine.isFeatureEnabled()) return;
        AppGroup foreground = engine.groupForPackage(currentForegroundPackage);
        String groupId = monitoredGroupId(foreground);
        if (groupId == null) return;
        Long delayMs = engine.nextCheckpointDelayMs(
                groupId, scheduler.elapsedRealtime());
        if (delayMs != null && delayMs == 0L) {
            onCheckpointDeadline(groupId);
        } else {
            rescheduleCheckpointDeadline();
        }
    }

    public synchronized void setRoleLabels(Map<String, String> labels) {
        overlay.setRoleLabels(labels);
    }

    public synchronized void debugSetLock(String groupId, int minutes, String roleId,
            String message, String commandId) {
        engine.setLock(groupId, minutes, roleId, message, commandId, now());
        persistRuntime();
        rescheduleCheckpointDeadline();
        if (engine.isFeatureEnabled()) notifySync("lock_change", groupId, 0L);
    }

    public synchronized void debugSetTemporaryUnlock(String groupId, int minutes,
            String roleId, String message, String commandId) {
        engine.setTemporaryUnlock(groupId, minutes, roleId, message, commandId, now());
        persistRuntime();
        if (engine.isFeatureEnabled()) notifySync("lock_change", groupId, 0L);
    }

    public synchronized void debugRemoveLock(String groupId) {
        SupervisionTime now = now();
        engine.removeLock(groupId, "debug-remove-" + now.getElapsedMs(), now);
        persistRuntime();
        if (engine.isFeatureEnabled()) notifySync("lock_change", groupId, 0L);
    }

    public synchronized CommandResult applyAiCommand(
            String action, String groupId, int minutes, String roleId,
            String message, String commandId, long expiresWallMs) {
        if (expiresWallMs < scheduler.currentTimeMillis()) {
            return new CommandResult(false, "expired");
        }
        if (!engine.isFeatureEnabled()) {
            return new CommandResult(false, "feature_disabled");
        }
        if (commandId == null || commandId.trim().isEmpty()) {
            return new CommandResult(false, "missing_command_id");
        }
        boolean deviceAction = "device_lock".equals(action)
                || "device_temp_unlock".equals(action)
                || "device_unlock".equals(action);
        String normalizedGroupId = groupId == null ? "" : groupId.trim();
        if (deviceAction && !normalizedGroupId.isEmpty()) {
            return new CommandResult(false, "device_group_must_be_empty");
        }
        AppGroup group = deviceAction ? null : engine.group(normalizedGroupId);
        if (!deviceAction && group == null) {
            return new CommandResult(false, "unknown_group");
        }
        SupervisionTime commandTime = now();
        try {
            if ("device_lock".equals(action)) {
                DeviceLockState.Snapshot before =
                        deviceLockState.snapshot(commandTime.getElapsedMs());
                if (before.getLock() != null
                        && commandId.equals(before.getLock().getCommandId())) {
                    return new CommandResult(true, "duplicate");
                }
                deviceLockState.setLock(TimedDirective.create(
                        commandTime.getElapsedMs(), commandTime.getWallMs(),
                        clampMinutes(minutes), roleId, message, commandId));
            } else if ("device_temp_unlock".equals(action)) {
                DeviceLockState.Snapshot before =
                        deviceLockState.snapshot(commandTime.getElapsedMs());
                if (before.getTemporaryUnlock() != null
                        && commandId.equals(before.getTemporaryUnlock().getCommandId())) {
                    return new CommandResult(true, "duplicate");
                }
                deviceLockState.setTemporaryUnlock(TimedDirective.create(
                        commandTime.getElapsedMs(), commandTime.getWallMs(),
                        clampMinutes(minutes), roleId, message, commandId));
            } else if ("device_unlock".equals(action)) {
                deviceLockState.removeLock();
            } else if ("lock".equals(action)) {
                AppGroupState.Snapshot before =
                        engine.snapshot(normalizedGroupId, commandTime.getElapsedMs());
                if (before.getLock() != null
                        && commandId.equals(before.getLock().getCommandId())) {
                    return new CommandResult(true, "duplicate");
                }
                engine.setLock(normalizedGroupId, clampMinutes(minutes), roleId, message,
                        commandId, commandTime);
            } else if ("temp_unlock".equals(action)) {
                AppGroupState.Snapshot before =
                        engine.snapshot(normalizedGroupId, commandTime.getElapsedMs());
                if (before.getTemporaryUnlock() != null
                        && commandId.equals(before.getTemporaryUnlock().getCommandId())) {
                    return new CommandResult(true, "duplicate");
                }
                engine.setTemporaryUnlock(normalizedGroupId, clampMinutes(minutes), roleId,
                        message, commandId, commandTime);
            } else if ("unlock".equals(action)) {
                engine.removeLock(normalizedGroupId, commandId, commandTime);
            } else {
                return new CommandResult(false, "unknown_action");
            }
            persistRuntime();
            rescheduleCheckpointDeadline();
            notifySync("lock_change", normalizedGroupId, 0L);
            AppGroup foreground = engine.groupForPackage(currentForegroundPackage);
            if (deviceAction) {
                foregroundGeneration++;
                evaluateOverlay(currentForegroundPackage, foreground, commandTime);
            } else if (foreground != null
                    && normalizedGroupId.equals(foreground.getGroupId())) {
                evaluateOverlay(currentForegroundPackage, foreground, commandTime);
            }
            return new CommandResult(true, "");
        } catch (Exception error) {
            return new CommandResult(false,
                    error.getMessage() == null ? "execution_failed" : error.getMessage());
        }
    }

    private static int clampMinutes(int minutes) {
        return Math.max(1, Math.min(120, minutes));
    }

    public synchronized EmergencyUnlockGate.Snapshot emergencyBegin(
            String groupId, String targetGroupId) {
        return emergencyGate.begin(groupId, targetGroupId, scheduler.elapsedRealtime());
    }

    public synchronized EmergencyUnlockGate.Snapshot emergencyHold(
            String groupId, String targetGroupId, boolean holding) {
        return emergencyGate.holdProgress(
                groupId, targetGroupId, holding, scheduler.elapsedRealtime());
    }

    public synchronized EmergencyUnlockGate.Snapshot emergencyReason(
            String groupId, String targetGroupId, String reason) {
        return emergencyGate.submitReason(
                groupId, targetGroupId, reason, scheduler.elapsedRealtime());
    }

    public synchronized EmergencyUnlockGate.Result emergencyConfirm(
            String groupId, String targetGroupId) {
        return emergencyGate.confirm(groupId, targetGroupId, scheduler.elapsedRealtime());
    }

    public synchronized EmergencyUnlockGate.Snapshot emergencySnapshot() {
        return emergencyGate.snapshot(scheduler.elapsedRealtime());
    }

    public synchronized List<AppSupervisionStore.LogRecord> logs() {
        return store.readLogs(scheduler.currentTimeMillis());
    }

    private void recordEngineEvents(List<EngineEvent> events) {
        for (EngineEvent event : events) {
            if (event.getType() == EngineEvent.Type.CHECKPOINT_REACHED) {
                store.appendLog(new AppSupervisionStore.LogRecord(
                        event.getWallMs(),
                        "checkpoint_reached",
                        event.getGroupId(),
                        "checkpoint_ms=" + event.getCheckpointMs()));
                notifySync("checkpoint", event.getGroupId(), event.getCheckpointMs());
            } else if (event.getType() == EngineEvent.Type.ROUND_RESET) {
                store.appendLog(new AppSupervisionStore.LogRecord(
                        event.getWallMs(),
                        "round_reset",
                        event.getGroupId(),
                        "idle window elapsed"));
                notifySync("round_reset", event.getGroupId(), 0L);
            }
        }
    }

    private void appendDiagnostic(String type, String groupId, String message) {
        store.appendLog(new AppSupervisionStore.LogRecord(
                scheduler.currentTimeMillis(), type, groupId, message));
    }

    private void appendRecoveryDiagnostic(
            RecoveryState state, String type, String message) {
        if (state == lastLoggedRecoveryState) return;
        lastLoggedRecoveryState = state;
        appendDiagnostic(type, "", message);
    }

    public long elapsedRealtime() { return scheduler.elapsedRealtime(); }
    public RecoveryState recoveryState() { return recovery.getState(); }
    public String recoveryDiagnostic() { return recovery.getDiagnostic(); }

    public boolean isProtectedPackage(String packageName) {
        if (packageName == null) return true;
        String normalized = packageName.trim().toLowerCase(java.util.Locale.US);
        if (PROTECTED_PACKAGES.contains(normalized)) return true;
        if (context != null && normalized.equals(
                context.getPackageName().toLowerCase(java.util.Locale.US))) return true;
        return normalized.contains("launcher") || normalized.contains("emergency");
    }

    private void evaluateOverlay(String packageName, AppGroup group, SupervisionTime now) {
        AppGroupState.Snapshot appSnapshot = group == null ? null : engine.snapshot(
                group.getGroupId(), now.getElapsedMs());
        EffectiveState appState = group == null ? EffectiveState.NORMAL
                : engine.effectiveState(group.getGroupId(), now.getElapsedMs());
        DeviceLockState.Snapshot deviceSnapshot =
                deviceLockState.snapshot(now.getElapsedMs());
        SupervisionOverlayDecision.Mode mode = SupervisionOverlayDecision.decide(
                SupervisionOverlayDecision.Input.builder(
                                context == null ? "com.aion.chat" : context.getPackageName())
                        .foregroundPackage(packageName)
                        .aionsHomeForeground(aionsHomeForeground)
                        .deviceState(deviceSnapshot.getEffectiveState())
                        .appState(appState)
                        .build());
        scheduleDeviceStateTransition(deviceSnapshot, now);
        if (mode == SupervisionOverlayDecision.Mode.DEVICE_LOCK) {
            activeLockGuardKey = "";
            overlay.evaluateDevice(deviceSnapshot.getLock(), now);
        } else if (mode == SupervisionOverlayDecision.Mode.APP_LOCK) {
            overlay.evaluate(packageName, appState, group, appSnapshot, now);
        } else if (appState == EffectiveState.TEMPORARILY_UNLOCKED
                && group != null && appSnapshot != null) {
            overlay.evaluate(packageName, appState, group, appSnapshot, now);
        } else {
            activeLockGuardKey = "";
            overlay.hide();
        }
        if (mode == SupervisionOverlayDecision.Mode.APP_LOCK
                && appSnapshot != null && appSnapshot.getLock() != null
                && screenReady) {
            startLockGuard(packageName, group, appSnapshot.getLock());
        }
        if (appState == EffectiveState.TEMPORARILY_UNLOCKED
                && appSnapshot != null && appSnapshot.getTemporaryUnlock() != null) {
            scheduleTemporaryUnlockExpiry(
                    packageName, group.getGroupId(),
                    appSnapshot.getTemporaryUnlock(), now);
        }
    }

    private void scheduleDeviceStateTransition(
            DeviceLockState.Snapshot snapshot, SupervisionTime now) {
        TimedDirective next = snapshot.getEffectiveState()
                == EffectiveState.TEMPORARILY_UNLOCKED
                ? snapshot.getTemporaryUnlock()
                : snapshot.getEffectiveState() == EffectiveState.LOCKED
                        ? snapshot.getLock() : null;
        if (next == null) {
            scheduledDeviceStateKey = "";
            return;
        }
        String key = snapshot.getEffectiveState().name() + "|" + next.getCommandId()
                + "|" + next.getDeadlineElapsedMs();
        if (key.equals(scheduledDeviceStateKey)) return;
        scheduledDeviceStateKey = key;
        long delayMs = Math.max(0L,
                next.getDeadlineElapsedMs() - now.getElapsedMs());
        scheduler.schedule(() -> {
            synchronized (AppSupervisionRuntime.this) {
                if (!key.equals(scheduledDeviceStateKey)) return;
                scheduledDeviceStateKey = "";
                AppGroup foreground =
                        engine.groupForPackage(currentForegroundPackage);
                evaluateOverlay(currentForegroundPackage, foreground, now());
            }
        }, delayMs);
    }

    private void startLockGuard(String packageName, AppGroup group,
            TimedDirective directive) {
        String key = packageName + "|" + group.getGroupId()
                + "|" + directive.getCommandId();
        if (key.equals(activeLockGuardKey)) return;
        activeLockGuardKey = key;
        scheduler.schedule(() -> runLockGuard(key, packageName, group.getGroupId(),
                directive.getCommandId()), LOCK_GUARD_INTERVAL_MS);
    }

    private void runLockGuard(String key, String packageName,
            String groupId, String commandId) {
        if (!screenReady || !key.equals(activeLockGuardKey)
                || !packageName.equals(currentForegroundPackage)) {
            if (key.equals(activeLockGuardKey)) activeLockGuardKey = "";
            return;
        }
        AppGroup group = engine.group(groupId);
        SupervisionTime check = now();
        if (group == null || engine.effectiveState(groupId, check.getElapsedMs())
                != EffectiveState.LOCKED) {
            activeLockGuardKey = "";
            return;
        }
        AppGroupState.Snapshot snapshot = engine.snapshot(groupId, check.getElapsedMs());
        if (snapshot.getLock() == null
                || !commandId.equals(snapshot.getLock().getCommandId())) {
            activeLockGuardKey = "";
            return;
        }
        overlay.evaluate(packageName, EffectiveState.LOCKED, group, snapshot, check);
        if (key.equals(activeLockGuardKey)) {
            scheduler.schedule(() -> runLockGuard(key, packageName, groupId, commandId),
                    LOCK_GUARD_INTERVAL_MS);
        }
    }

    private void scheduleTemporaryUnlockExpiry(String packageName, String groupId,
            TimedDirective directive, SupervisionTime now) {
        String commandId = directive.getCommandId();
        if (commandId.equals(scheduledTemporaryUnlockCommandId)) return;
        scheduledTemporaryUnlockCommandId = commandId;
        long delayMs = Math.max(0L, directive.getDeadlineElapsedMs() - now.getElapsedMs());
        scheduler.schedule(() -> {
            if (!screenReady || !packageName.equals(currentForegroundPackage)
                    || !commandId.equals(scheduledTemporaryUnlockCommandId)) return;
            scheduledTemporaryUnlockCommandId = "";
            AppGroup currentGroup = engine.group(groupId);
            if (currentGroup != null) evaluateOverlay(packageName, currentGroup, now());
        }, delayMs);
    }

    private void startDetector() {
        if (detectorPolling || !screenReady || accessibilityActive) {
            return;
        }
        detectorPolling = true;
        detector.start(this::onPackageForeground);
    }

    private void stopDetectorIfRunning() {
        if (!detectorPolling) {
            return;
        }
        detector.stop();
        detectorPolling = false;
    }

    private void scheduleUsagePersistence() {
        if (persistenceScheduled) {
            return;
        }
        persistenceScheduled = true;
        scheduler.schedule(() -> {
            persistenceScheduled = false;
            persistRuntime();
        }, 60_000L);
    }

    private void rescheduleCheckpointDeadline() {
        AppGroup foreground = engine.groupForPackage(currentForegroundPackage);
        String groupId = monitoredGroupId(foreground);
        if (!screenReady || groupId == null) {
            checkpointDeadlineScheduler.cancel();
            return;
        }
        scheduleCheckpointDeadline(groupId);
    }

    private void scheduleCheckpointDeadline(String groupId) {
        Long delayMs = engine.nextCheckpointDelayMs(
                groupId, scheduler.elapsedRealtime());
        if (delayMs == null) {
            checkpointDeadlineScheduler.cancel();
            return;
        }
        checkpointDeadlineScheduler.replace(
                () -> onCheckpointDeadline(groupId), delayMs);
    }

    private synchronized void onCheckpointDeadline(String groupId) {
        if (!screenReady || !engine.isFeatureEnabled()) return;
        AppGroup foreground = engine.groupForPackage(currentForegroundPackage);
        if (foreground == null || !foreground.isMonitored()
                || !groupId.equals(foreground.getGroupId())) return;
        SupervisionTime tick = now();
        List<EngineEvent> events = engine.onUsageTick(
                tick.getElapsedMs(), tick.getWallMs());
        if (!events.isEmpty()) recordEngineEvents(events);
        persistRuntime();
        scheduleCheckpointDeadline(groupId);
    }

    private String monitoredGroupId(AppGroup group) {
        return engine.isFeatureEnabled() && group != null && group.isMonitored()
                ? group.getGroupId() : null;
    }

    private static boolean sameGroup(String left, String right) {
        return left == null ? right == null : left.equals(right);
    }

    private void notifySync(String eventType, String groupId, long checkpointMs) {
        SyncListener listener = syncListener;
        if (listener != null) listener.onStateEvent(eventType, groupId, checkpointMs);
    }

    private void persistRuntime() {
        long nowElapsedMs = scheduler.elapsedRealtime();
        Map<String, AppSupervisionStore.PersistedGroupState> values = new LinkedHashMap<>();
        for (Map.Entry<String, AppGroupState.Snapshot> entry
                : engine.snapshots(nowElapsedMs).entrySet()) {
            AppGroupState.Snapshot snapshot = entry.getValue();
            values.put(entry.getKey(), new AppSupervisionStore.PersistedGroupState(
                    snapshot.getRoundUsageMs(),
                    snapshot.getLastExitElapsedMs(),
                    snapshot.getFiredCheckpointsMs(),
                    snapshot.getLock(),
                    snapshot.getTemporaryUnlock()));
        }
        store.saveBootScopedState(
                bootId, new AppSupervisionStore.RuntimeSnapshot(
                        values,
                        new AppSupervisionStore.PersistedDeviceState(
                                deviceLockState.snapshot(nowElapsedMs).getLock(),
                                deviceLockState.snapshot(nowElapsedMs)
                                        .getTemporaryUnlock())));
    }

    private void persistConfig() {
        store.saveConfig(new AppSupervisionStore.ConfigSnapshot(
                engine.isFeatureEnabled(), engine.groups()));
    }

    private void restoreRuntime() {
        AppSupervisionStore.RuntimeSnapshot persisted = store.loadBootScopedState(bootId);
        deviceLockState.restore(
                persisted.getDeviceState().getLock(),
                persisted.getDeviceState().getTemporaryUnlock());
        for (Map.Entry<String, AppSupervisionStore.PersistedGroupState> entry
                : persisted.getStates().entrySet()) {
            AppSupervisionStore.PersistedGroupState value = entry.getValue();
            engine.restoreGroupState(
                    entry.getKey(),
                    value.getRoundUsageMs(),
                    value.getLastExitElapsedMs(),
                    value.getFiredCheckpointsMs(),
                    value.getLock(),
                    value.getTemporaryUnlock());
        }
    }

    private SupervisionTime now() {
        return new SupervisionTime(
                scheduler.elapsedRealtime(), scheduler.currentTimeMillis());
    }

    private void shutdown() {
        stopDetectorIfRunning();
        checkpointDeadlineScheduler.shutdown();
        scheduler.cancelAll();
        overlay.hide();
        persistRuntime();
    }

    private static String readBootId(Context context) {
        try {
            return String.valueOf(Settings.Global.getInt(
                    context.getContentResolver(), Settings.Global.BOOT_COUNT));
        } catch (Exception ignored) {
            return "elapsed-" + (SystemClock.elapsedRealtime() / (24L * 60L * 60L * 1000L));
        }
    }

    private static boolean isAccessibilityServiceEnabled(Context context) {
        try {
            if (Settings.Secure.getInt(
                    context.getContentResolver(),
                    Settings.Secure.ACCESSIBILITY_ENABLED,
                    0) != 1) {
                return false;
            }
            String enabled = Settings.Secure.getString(
                    context.getContentResolver(),
                    Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            String target = new android.content.ComponentName(
                    context, com.aion.chat.AionAccessibilityService.class)
                    .flattenToString();
            if (enabled == null) return false;
            for (String component : enabled.split(":")) {
                if (target.equals(component.trim())) return true;
            }
            return false;
        } catch (Exception ignored) {
            return false;
        }
    }

    private static final class MainScheduler implements Scheduler {
        private final Handler handler = new Handler(Looper.getMainLooper());
        @Override public long elapsedRealtime() { return SystemClock.elapsedRealtime(); }
        @Override public long currentTimeMillis() { return System.currentTimeMillis(); }
        @Override public void schedule(Runnable runnable, long delayMs) {
            handler.postDelayed(runnable, delayMs);
        }
        @Override public void cancelAll() { handler.removeCallbacksAndMessages(null); }
    }
}
