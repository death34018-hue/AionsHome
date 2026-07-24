package com.aion.chat.supervision;

public final class EmergencyUnlockGate {
    public interface LockController {
        boolean isLocked(String groupId);
        void removeLock(String groupId);
    }

    public interface AuditSink {
        void record(String event, String groupId, String reason);
    }

    public enum Phase {
        IDLE,
        HOLDING,
        REASON_REQUIRED,
        WAITING,
        READY,
        COMPLETED,
        CANCELLED
    }

    private static final long HOLD_MS = 8_000L;
    private static final long WAIT_MS = 60_000L;

    private final LockController locks;
    private final AuditSink audit;
    private Phase phase = Phase.IDLE;
    private String groupId = "";
    private String reason = "";
    private String error = "";
    private long holdStartedMs;
    private long heldMs;
    private long waitDeadlineMs;

    public EmergencyUnlockGate(LockController locks, AuditSink audit) {
        if (locks == null || audit == null) {
            throw new IllegalArgumentException("gate collaborators are required");
        }
        this.locks = locks;
        this.audit = audit;
    }

    public Snapshot begin(String requestedGroupId, String currentTargetGroupId, long nowElapsedMs) {
        if (!sameTarget(requestedGroupId, currentTargetGroupId)
                || !locks.isLocked(requestedGroupId)) {
            return cancel(requestedGroupId, "target_mismatch", nowElapsedMs);
        }
        groupId = requestedGroupId;
        reason = "";
        error = "";
        holdStartedMs = nowElapsedMs;
        heldMs = 0L;
        waitDeadlineMs = 0L;
        phase = Phase.HOLDING;
        audit.record("started", groupId, "");
        return snapshot(nowElapsedMs);
    }

    public Snapshot holdProgress(String requestedGroupId, String currentTargetGroupId,
            boolean holding, long nowElapsedMs) {
        if (!holding || phase != Phase.HOLDING
                || !sameTarget(requestedGroupId, currentTargetGroupId)
                || !requestedGroupId.equals(groupId)
                || !locks.isLocked(groupId)) {
            return cancel(groupId.isEmpty() ? requestedGroupId : groupId,
                    holding ? "target_changed" : "hold_interrupted", nowElapsedMs);
        }
        heldMs = Math.max(0L, nowElapsedMs - holdStartedMs);
        if (heldMs >= HOLD_MS) {
            heldMs = HOLD_MS;
            phase = Phase.REASON_REQUIRED;
        }
        return snapshot(nowElapsedMs);
    }

    public Snapshot submitReason(String requestedGroupId, String currentTargetGroupId,
            String submittedReason, long nowElapsedMs) {
        if (phase != Phase.REASON_REQUIRED
                || !sameTarget(requestedGroupId, currentTargetGroupId)
                || !requestedGroupId.equals(groupId)) {
            return cancel(groupId.isEmpty() ? requestedGroupId : groupId,
                    "target_changed", nowElapsedMs);
        }
        String checkedReason = submittedReason == null ? "" : submittedReason.trim();
        if (checkedReason.isEmpty()) {
            error = "reason is required";
            return snapshot(nowElapsedMs);
        }
        reason = checkedReason;
        error = "";
        waitDeadlineMs = nowElapsedMs + WAIT_MS;
        phase = Phase.WAITING;
        return snapshot(nowElapsedMs);
    }

    public long remainingDelay(long nowElapsedMs) {
        if (phase != Phase.WAITING && phase != Phase.READY) {
            return 0L;
        }
        long remaining = Math.max(0L, waitDeadlineMs - nowElapsedMs);
        if (remaining == 0L) phase = Phase.READY;
        return remaining;
    }

    public Result confirm(String requestedGroupId, String currentTargetGroupId,
            long nowElapsedMs) {
        if (!sameTarget(requestedGroupId, currentTargetGroupId)
                || !requestedGroupId.equals(groupId)) {
            cancel(groupId.isEmpty() ? requestedGroupId : groupId,
                    "target_changed", nowElapsedMs);
            return new Result(false, "target changed", snapshot(nowElapsedMs));
        }
        if (phase != Phase.WAITING && phase != Phase.READY) {
            return new Result(false, "gate is not waiting", snapshot(nowElapsedMs));
        }
        if (remainingDelay(nowElapsedMs) > 0L) {
            return new Result(false, "waiting period is active", snapshot(nowElapsedMs));
        }
        if (!locks.isLocked(groupId)) {
            cancel(groupId, "lock_missing", nowElapsedMs);
            return new Result(false, "lock is no longer active", snapshot(nowElapsedMs));
        }
        locks.removeLock(groupId);
        phase = Phase.COMPLETED;
        audit.record("completed", groupId, reason);
        return new Result(true, "", snapshot(nowElapsedMs));
    }

    public Snapshot snapshot(long nowElapsedMs) {
        long remaining = (phase == Phase.WAITING || phase == Phase.READY)
                ? Math.max(0L, waitDeadlineMs - nowElapsedMs) : 0L;
        if (phase == Phase.WAITING && remaining == 0L) phase = Phase.READY;
        return new Snapshot(phase, groupId, heldMs, remaining, reason, error);
    }

    private Snapshot cancel(String cancelledGroupId, String why, long nowElapsedMs) {
        groupId = cancelledGroupId == null ? "" : cancelledGroupId;
        error = why;
        phase = Phase.CANCELLED;
        audit.record("cancelled", groupId, why);
        return snapshot(nowElapsedMs);
    }

    private static boolean sameTarget(String requested, String current) {
        return requested != null && !requested.trim().isEmpty() && requested.equals(current);
    }

    public static final class Snapshot {
        private final Phase phase;
        private final String groupId;
        private final long heldMs;
        private final long remainingDelayMs;
        private final String reason;
        private final String error;

        Snapshot(Phase phase, String groupId, long heldMs, long remainingDelayMs,
                String reason, String error) {
            this.phase = phase;
            this.groupId = groupId;
            this.heldMs = heldMs;
            this.remainingDelayMs = remainingDelayMs;
            this.reason = reason;
            this.error = error;
        }

        public Phase getPhase() { return phase; }
        public String getGroupId() { return groupId; }
        public long getHeldMs() { return heldMs; }
        public long getRemainingDelayMs() { return remainingDelayMs; }
        public String getReason() { return reason; }
        public String getError() { return error; }
    }

    public static final class Result {
        private final boolean ok;
        private final String error;
        private final Snapshot snapshot;

        Result(boolean ok, String error, Snapshot snapshot) {
            this.ok = ok;
            this.error = error;
            this.snapshot = snapshot;
        }

        public boolean isOk() { return ok; }
        public String getError() { return error; }
        public Snapshot getSnapshot() { return snapshot; }
    }
}
