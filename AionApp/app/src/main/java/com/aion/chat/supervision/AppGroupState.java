package com.aion.chat.supervision;

import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;

public final class AppGroupState {
    private long roundUsageMs;
    private Long foregroundStartedElapsedMs;
    private Long lastExitElapsedMs;
    private final LinkedHashSet<Long> firedCheckpointsMs = new LinkedHashSet<>();
    private TimedDirective lock;
    private TimedDirective temporaryUnlock;

    void open(long elapsedMs) {
        if (foregroundStartedElapsedMs == null) {
            foregroundStartedElapsedMs = elapsedMs;
        }
    }

    long close(long elapsedMs) {
        if (foregroundStartedElapsedMs == null) {
            return 0L;
        }
        long duration = Math.max(0L, elapsedMs - foregroundStartedElapsedMs);
        roundUsageMs += duration;
        foregroundStartedElapsedMs = null;
        lastExitElapsedMs = elapsedMs;
        return duration;
    }

    boolean shouldReset(long elapsedMs, long idleResetMs) {
        return lastExitElapsedMs != null && elapsedMs - lastExitElapsedMs >= idleResetMs;
    }

    void resetRound() {
        roundUsageMs = 0L;
        firedCheckpointsMs.clear();
        lastExitElapsedMs = null;
    }

    void resetRound(long elapsedMs) {
        boolean wasOpen = foregroundStartedElapsedMs != null;
        resetRound();
        if (wasOpen) foregroundStartedElapsedMs = elapsedMs;
    }

    long usageAt(long nowElapsedMs) {
        if (foregroundStartedElapsedMs == null) {
            return roundUsageMs;
        }
        return roundUsageMs + Math.max(0L, nowElapsedMs - foregroundStartedElapsedMs);
    }

    boolean markCheckpoint(long checkpointMs) {
        return firedCheckpointsMs.add(checkpointMs);
    }

    void setLock(TimedDirective lock) { this.lock = lock; }
    void setTemporaryUnlock(TimedDirective temporaryUnlock) {
        this.temporaryUnlock = temporaryUnlock;
    }
    void removeLock() { lock = null; }

    void restore(long roundUsageMs, Long lastExitElapsedMs,
            Set<Long> firedCheckpointsMs, TimedDirective lock,
            TimedDirective temporaryUnlock) {
        this.roundUsageMs = Math.max(0L, roundUsageMs);
        this.foregroundStartedElapsedMs = null;
        this.lastExitElapsedMs = lastExitElapsedMs;
        this.firedCheckpointsMs.clear();
        this.firedCheckpointsMs.addAll(firedCheckpointsMs);
        this.lock = lock;
        this.temporaryUnlock = temporaryUnlock;
    }

    TimedDirective lock() { return lock; }
    TimedDirective temporaryUnlock() { return temporaryUnlock; }

    Snapshot snapshot(long nowElapsedMs) {
        return new Snapshot(
                usageAt(nowElapsedMs),
                foregroundStartedElapsedMs != null,
                lastExitElapsedMs,
                firedCheckpointsMs,
                lock,
                temporaryUnlock);
    }

    public static final class Snapshot {
        private final long roundUsageMs;
        private final boolean foregroundOpen;
        private final Long lastExitElapsedMs;
        private final Set<Long> firedCheckpointsMs;
        private final TimedDirective lock;
        private final TimedDirective temporaryUnlock;

        Snapshot(long roundUsageMs, boolean foregroundOpen, Long lastExitElapsedMs,
                Set<Long> firedCheckpointsMs, TimedDirective lock,
                TimedDirective temporaryUnlock) {
            this.roundUsageMs = roundUsageMs;
            this.foregroundOpen = foregroundOpen;
            this.lastExitElapsedMs = lastExitElapsedMs;
            this.firedCheckpointsMs = Collections.unmodifiableSet(
                    new LinkedHashSet<>(firedCheckpointsMs));
            this.lock = lock;
            this.temporaryUnlock = temporaryUnlock;
        }

        public long getRoundUsageMs() { return roundUsageMs; }
        public boolean isForegroundOpen() { return foregroundOpen; }
        public Long getLastExitElapsedMs() { return lastExitElapsedMs; }
        public Set<Long> getFiredCheckpointsMs() { return firedCheckpointsMs; }
        public TimedDirective getLock() { return lock; }
        public TimedDirective getTemporaryUnlock() { return temporaryUnlock; }
    }
}
