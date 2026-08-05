package com.aion.chat.supervision;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class AppSupervisionEngine {
    private boolean featureEnabled;
    private final LinkedHashMap<String, AppGroup> groups = new LinkedHashMap<>();
    private final LinkedHashMap<String, String> packageToGroup = new LinkedHashMap<>();
    private final LinkedHashMap<String, AppGroupState> states = new LinkedHashMap<>();
    private String foregroundGroupId;

    public AppSupervisionEngine(boolean featureEnabled, List<AppGroup> initialGroups) {
        this.featureEnabled = featureEnabled;
        if (initialGroups == null) {
            throw new IllegalArgumentException("initialGroups is required");
        }
        for (AppGroup group : initialGroups) {
            upsertGroup(group);
        }
    }

    public List<EngineEvent> onForegroundChanged(
            String packageName, long elapsedMs, long wallMs) {
        String nextGroupId = packageToGroup.get(packageName);
        if (!featureEnabled || (nextGroupId != null && !groups.get(nextGroupId).isMonitored())) {
            nextGroupId = null;
        }
        if (same(foregroundGroupId, nextGroupId)) {
            return Collections.emptyList();
        }

        ArrayList<EngineEvent> events = new ArrayList<>();
        closeForeground(elapsedMs, wallMs, events);
        events.addAll(expireIdleRounds(elapsedMs, wallMs));
        if (nextGroupId != null) {
            AppGroupState state = state(nextGroupId);
            state.open(elapsedMs);
            foregroundGroupId = nextGroupId;
        }
        return events;
    }

    public List<EngineEvent> onScreenOff(long elapsedMs, long wallMs) {
        ArrayList<EngineEvent> events = new ArrayList<>();
        closeForeground(elapsedMs, wallMs, events);
        return events;
    }

    public List<EngineEvent> onUserPresent(long elapsedMs, long wallMs) {
        return Collections.emptyList();
    }

    public List<EngineEvent> expireIdleRounds(long elapsedMs, long wallMs) {
        if (!featureEnabled) {
            return Collections.emptyList();
        }
        ArrayList<EngineEvent> events = new ArrayList<>();
        for (Map.Entry<String, AppGroupState> entry : states.entrySet()) {
            String groupId = entry.getKey();
            if (groupId.equals(foregroundGroupId)) continue;
            AppGroup group = groups.get(groupId);
            if (group == null) continue;
            AppGroupState state = entry.getValue();
            if (state.shouldReset(elapsedMs, group.getPolicy().getIdleResetMs())) {
                state.resetRound();
                events.add(EngineEvent.roundReset(groupId, elapsedMs, wallMs));
            }
        }
        return events;
    }

    public List<EngineEvent> onUsageTick(long elapsedMs, long wallMs) {
        if (!featureEnabled || foregroundGroupId == null) {
            return Collections.emptyList();
        }
        ArrayList<EngineEvent> events = new ArrayList<>();
        collectCheckpoints(foregroundGroupId, elapsedMs, wallMs, events);
        return events;
    }

    public Long nextCheckpointDelayMs(String groupId, long elapsedMs) {
        if (!featureEnabled || groupId == null || !groupId.equals(foregroundGroupId)) {
            return null;
        }
        AppGroup group = groups.get(groupId);
        AppGroupState state = states.get(groupId);
        if (group == null || state == null || !group.isMonitored()) {
            return null;
        }
        AppGroupState.Snapshot snapshot = state.snapshot(elapsedMs);
        for (Long checkpointMs : group.getPolicy().getCheckpointsMs()) {
            if (!snapshot.getFiredCheckpointsMs().contains(checkpointMs)) {
                return Math.max(0L, checkpointMs - snapshot.getRoundUsageMs());
            }
        }
        return null;
    }

    public void setLock(String groupId, int minutes, String roleId, String message,
            String commandId, SupervisionTime now) {
        AppGroupState target = state(required(groupId));
        target.setLock(TimedDirective.create(
                now.getElapsedMs(), now.getWallMs(), minutes, roleId, message, commandId));
        target.resetRound(now.getElapsedMs());
    }

    public void setTemporaryUnlock(String groupId, int minutes, String roleId,
            String message, String commandId, SupervisionTime now) {
        state(required(groupId)).setTemporaryUnlock(TimedDirective.create(
                now.getElapsedMs(), now.getWallMs(), minutes, roleId, message, commandId));
    }

    public void removeLock(String groupId, String commandId, SupervisionTime now) {
        required(commandId);
        if (now == null) {
            throw new IllegalArgumentException("now is required");
        }
        state(required(groupId)).removeLock();
    }

    public EffectiveState effectiveState(String groupId, long nowElapsedMs) {
        AppGroupState state = states.get(groupId);
        if (state == null) {
            return EffectiveState.NORMAL;
        }
        TimedDirective temporaryUnlock = state.temporaryUnlock();
        if (temporaryUnlock != null && temporaryUnlock.isActive(nowElapsedMs)) {
            return EffectiveState.TEMPORARILY_UNLOCKED;
        }
        TimedDirective lock = state.lock();
        if (lock != null && lock.isActive(nowElapsedMs)) {
            return EffectiveState.LOCKED;
        }
        return EffectiveState.NORMAL;
    }

    public AppGroupState.Snapshot snapshot(String groupId, long nowElapsedMs) {
        return state(required(groupId)).snapshot(nowElapsedMs);
    }

    public void setFeatureEnabled(boolean enabled) {
        featureEnabled = enabled;
    }

    public boolean isFeatureEnabled() {
        return featureEnabled;
    }

    public void upsertGroup(AppGroup group) {
        if (group == null) {
            throw new IllegalArgumentException("group is required");
        }
        AppGroup old = groups.put(group.getGroupId(), group);
        if (old != null) {
            for (String packageName : old.getPackageNames()) {
                packageToGroup.remove(packageName);
            }
        }
        for (String packageName : group.getPackageNames()) {
            String existing = packageToGroup.put(packageName, group.getGroupId());
            if (existing != null && !existing.equals(group.getGroupId())) {
                throw new IllegalArgumentException("package belongs to another group");
            }
        }
        state(group.getGroupId());
    }

    public void removeGroup(String groupId, long nowElapsedMs) {
        String requiredGroupId = required(groupId);
        AppGroupState existingState = states.get(requiredGroupId);
        TimedDirective lock = existingState == null ? null : existingState.lock();
        if (lock != null && lock.isActive(nowElapsedMs)) {
            throw new IllegalStateException("active lock must be removed first");
        }
        AppGroup removed = groups.remove(requiredGroupId);
        if (removed != null) {
            for (String packageName : removed.getPackageNames()) {
                packageToGroup.remove(packageName);
            }
        }
        states.remove(requiredGroupId);
        if (requiredGroupId.equals(foregroundGroupId)) {
            foregroundGroupId = null;
        }
    }

    public boolean hasGroup(String groupId) {
        return groups.containsKey(groupId);
    }

    public void clearRound(String groupId) {
        state(required(groupId)).resetRound();
        if (groupId.equals(foregroundGroupId)) foregroundGroupId = null;
    }

    public void restoreGroupState(String groupId, long roundUsageMs,
            Long lastExitElapsedMs, java.util.Set<Long> firedCheckpointsMs,
            TimedDirective lock, TimedDirective temporaryUnlock) {
        if (firedCheckpointsMs == null) {
            throw new IllegalArgumentException("firedCheckpointsMs is required");
        }
        state(required(groupId)).restore(
                roundUsageMs, lastExitElapsedMs, firedCheckpointsMs, lock, temporaryUnlock);
    }

    public List<AppGroup> groups() {
        return Collections.unmodifiableList(new ArrayList<>(groups.values()));
    }

    public AppGroup group(String groupId) {
        return groups.get(groupId);
    }

    public AppGroup groupForPackage(String packageName) {
        String groupId = packageToGroup.get(packageName);
        return groupId == null ? null : groups.get(groupId);
    }

    public Map<String, AppGroupState.Snapshot> snapshots(long nowElapsedMs) {
        LinkedHashMap<String, AppGroupState.Snapshot> result = new LinkedHashMap<>();
        for (Map.Entry<String, AppGroupState> entry : states.entrySet()) {
            result.put(entry.getKey(), entry.getValue().snapshot(nowElapsedMs));
        }
        return Collections.unmodifiableMap(result);
    }

    private void closeForeground(long elapsedMs, long wallMs, List<EngineEvent> events) {
        if (foregroundGroupId == null) {
            return;
        }
        String closingGroupId = foregroundGroupId;
        foregroundGroupId = null;
        AppGroupState state = state(closingGroupId);
        state.close(elapsedMs);
        events.add(EngineEvent.intervalClosed(closingGroupId, elapsedMs, wallMs));
        collectCheckpoints(closingGroupId, elapsedMs, wallMs, events);
    }

    private void collectCheckpoints(
            String groupId, long elapsedMs, long wallMs, List<EngineEvent> events) {
        AppGroup group = groups.get(groupId);
        if (group == null) return;
        AppGroupState state = state(groupId);
        long usageMs = state.usageAt(elapsedMs);
        for (Long checkpointMs : group.getPolicy().getCheckpointsMs()) {
            if (usageMs >= checkpointMs && state.markCheckpoint(checkpointMs)) {
                events.add(EngineEvent.checkpoint(groupId, elapsedMs, wallMs, checkpointMs));
            }
        }
    }

    private AppGroupState state(String groupId) {
        AppGroupState state = states.get(groupId);
        if (state == null) {
            state = new AppGroupState();
            states.put(groupId, state);
        }
        return state;
    }

    private static boolean same(String left, String right) {
        return left == null ? right == null : left.equals(right);
    }

    private static String required(String value) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("value is required");
        }
        return value.trim();
    }
}
