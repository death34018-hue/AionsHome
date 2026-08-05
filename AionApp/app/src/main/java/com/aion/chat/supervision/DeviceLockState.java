package com.aion.chat.supervision;

public final class DeviceLockState {
    private TimedDirective lock;
    private TimedDirective temporaryUnlock;

    public void setLock(TimedDirective value) {
        lock = required(value);
    }

    public void setTemporaryUnlock(TimedDirective value) {
        temporaryUnlock = required(value);
    }

    public void removeLock() {
        lock = null;
    }

    public EffectiveState effectiveState(long nowElapsedMs) {
        if (temporaryUnlock != null && temporaryUnlock.isActive(nowElapsedMs)) {
            return EffectiveState.TEMPORARILY_UNLOCKED;
        }
        if (lock != null && lock.isActive(nowElapsedMs)) {
            return EffectiveState.LOCKED;
        }
        return EffectiveState.NORMAL;
    }

    public Snapshot snapshot(long nowElapsedMs) {
        return new Snapshot(lock, temporaryUnlock, effectiveState(nowElapsedMs));
    }

    public void restore(
            TimedDirective restoredLock,
            TimedDirective restoredTemporaryUnlock) {
        lock = restoredLock;
        temporaryUnlock = restoredTemporaryUnlock;
    }

    private static TimedDirective required(TimedDirective value) {
        if (value == null) {
            throw new IllegalArgumentException("directive is required");
        }
        return value;
    }

    public static final class Snapshot {
        private final TimedDirective lock;
        private final TimedDirective temporaryUnlock;
        private final EffectiveState effectiveState;

        Snapshot(
                TimedDirective lock,
                TimedDirective temporaryUnlock,
                EffectiveState effectiveState) {
            this.lock = lock;
            this.temporaryUnlock = temporaryUnlock;
            this.effectiveState = effectiveState;
        }

        public TimedDirective getLock() {
            return lock;
        }

        public TimedDirective getTemporaryUnlock() {
            return temporaryUnlock;
        }

        public EffectiveState getEffectiveState() {
            return effectiveState;
        }
    }
}
