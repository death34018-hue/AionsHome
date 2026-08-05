package com.aion.chat.supervision;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

public class DeviceLockStateTest {
    private static final long MINUTE_MS = 60_000L;

    @Test
    public void laterDeviceLockReplacesEarlierWithoutChangingTemporaryUnlock() {
        DeviceLockState state = new DeviceLockState();
        state.setLock(directive("first", 10));
        state.setTemporaryUnlock(directive("temporary", 5));

        state.setLock(directive("second", 20));

        DeviceLockState.Snapshot snapshot = state.snapshot(0L);
        assertEquals("second", snapshot.getLock().getCommandId());
        assertEquals("temporary", snapshot.getTemporaryUnlock().getCommandId());
    }

    @Test
    public void activeTemporaryUnlockOverridesLockUntilItsOwnDeadline() {
        DeviceLockState state = new DeviceLockState();
        state.setLock(directive("lock", 60));
        state.setTemporaryUnlock(directive("temporary", 10));

        assertEquals(EffectiveState.TEMPORARILY_UNLOCKED,
                state.effectiveState(5 * MINUTE_MS));
        assertEquals(EffectiveState.LOCKED,
                state.effectiveState(11 * MINUTE_MS));
    }

    @Test
    public void removingDeviceLockDoesNotMutateTemporaryUnlock() {
        DeviceLockState state = new DeviceLockState();
        state.setLock(directive("lock", 60));
        state.setTemporaryUnlock(directive("temporary", 10));

        state.removeLock();

        DeviceLockState.Snapshot snapshot = state.snapshot(0L);
        assertNull(snapshot.getLock());
        assertEquals("temporary", snapshot.getTemporaryUnlock().getCommandId());
    }

    private static TimedDirective directive(String commandId, int minutes) {
        return TimedDirective.create(
                0L, 1_800_000_000_000L, minutes,
                "role-main", "message-" + commandId, commandId);
    }
}
