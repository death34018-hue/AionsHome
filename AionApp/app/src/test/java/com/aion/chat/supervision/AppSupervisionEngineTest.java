package com.aion.chat.supervision;

import org.junit.Test;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class AppSupervisionEngineTest {
    private static final long MINUTE_MS = 60_000L;
    private static final long WALL_BASE = 1_800_000_000_000L;

    @Test
    public void checkpointFiresOnceWhenUsageCrossesTwentyMinutes() {
        AppSupervisionEngine engine = engine(30, 20);

        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        List<EngineEvent> first = engine.onForegroundChanged(
                "com.example.other", elapsedAt(21), wallAt(21));
        engine.onForegroundChanged("com.example.main", elapsedAt(22), wallAt(22));
        List<EngineEvent> second = engine.onForegroundChanged(
                "com.example.other", elapsedAt(30), wallAt(30));

        assertEquals(1, checkpointCount(first) + checkpointCount(second));
        assertEquals(29 * MINUTE_MS, engine.snapshot("group-1", elapsedAt(30)).getRoundUsageMs());
    }

    @Test
    public void foregroundUsageTickFiresTwentyAndFortyWithoutExitOrDuplicates() {
        AppSupervisionEngine engine = engine(30, 20, 40);
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));

        assertEquals(0, checkpointCount(engine.onUsageTick(elapsedAt(10), wallAt(10))));
        assertEquals(1, checkpointCount(engine.onUsageTick(elapsedAt(20), wallAt(20))));
        assertEquals(0, checkpointCount(engine.onUsageTick(elapsedAt(21), wallAt(21))));
        assertEquals(1, checkpointCount(engine.onUsageTick(elapsedAt(40), wallAt(40))));
        assertEquals(0, checkpointCount(engine.onUsageTick(elapsedAt(41), wallAt(41))));
    }

    @Test
    public void nextCheckpointDelayUsesLiveUsageAndSkipsFiredCheckpoints() {
        AppSupervisionEngine engine = engine(30, 20, 40);
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));

        assertEquals(Long.valueOf(15 * MINUTE_MS),
                engine.nextCheckpointDelayMs("group-1", elapsedAt(5)));

        engine.onUsageTick(elapsedAt(20), wallAt(20));
        assertEquals(Long.valueOf(20 * MINUTE_MS),
                engine.nextCheckpointDelayMs("group-1", elapsedAt(20)));

        engine.onUsageTick(elapsedAt(40), wallAt(40));
        assertEquals(null,
                engine.nextCheckpointDelayMs("group-1", elapsedAt(40)));
    }

    @Test
    public void successfulLockClearsRoundAndRestartsOpenIntervalAtCommandTime() {
        AppSupervisionEngine engine = engine(30, 20, 40);
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        engine.onUsageTick(elapsedAt(20), wallAt(20));

        engine.setLock("group-1", 60, "role-main", "休息", "cmd-lock", at(25));

        AppGroupState.Snapshot locked = engine.snapshot("group-1", elapsedAt(25));
        assertEquals(0L, locked.getRoundUsageMs());
        assertTrue(locked.getFiredCheckpointsMs().isEmpty());
        assertTrue(locked.isForegroundOpen());
        assertEquals(5 * MINUTE_MS,
                engine.snapshot("group-1", elapsedAt(30)).getRoundUsageMs());
    }

    @Test
    public void exitAndReenterBeforeIdleResetContinuesRound() {
        AppSupervisionEngine engine = engine(30, 20, 40);

        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        engine.onForegroundChanged("com.example.other", elapsedAt(10), wallAt(10));
        engine.onForegroundChanged("com.example.main", elapsedAt(25), wallAt(25));
        engine.onForegroundChanged("com.example.other", elapsedAt(35), wallAt(35));

        assertEquals(20 * MINUTE_MS, engine.snapshot("group-1", elapsedAt(35)).getRoundUsageMs());
        assertTrue(engine.snapshot("group-1", elapsedAt(35)).getFiredCheckpointsMs()
                .contains(20 * MINUTE_MS));
    }

    @Test
    public void fullIdleWindowClearsUsageAndFiredCheckpoints() {
        AppSupervisionEngine engine = engine(30, 5);

        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        engine.onForegroundChanged("com.example.other", elapsedAt(10), wallAt(10));
        engine.onForegroundChanged("com.example.main", elapsedAt(40), wallAt(40));

        AppGroupState.Snapshot snapshot = engine.snapshot("group-1", elapsedAt(40));
        assertEquals(0L, snapshot.getRoundUsageMs());
        assertTrue(snapshot.getFiredCheckpointsMs().isEmpty());
        assertTrue(snapshot.isForegroundOpen());
    }

    @Test
    public void idleExpirationClearsRoundAtExactBoundaryAndIsIdempotent() {
        AppSupervisionEngine engine = engine(30, 5);
        engine.setLock("group-1", 60, "role-main", "rest", "lock-idle", at(0));
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        engine.onForegroundChanged("com.example.other", elapsedAt(10), wallAt(10));

        assertTrue(engine.expireIdleRounds(
                elapsedAt(39), wallAt(39)).isEmpty());
        List<EngineEvent> expired = engine.expireIdleRounds(
                elapsedAt(40), wallAt(40));

        assertEquals(1, expired.size());
        assertEquals(EngineEvent.Type.ROUND_RESET, expired.get(0).getType());
        AppGroupState.Snapshot snapshot = engine.snapshot("group-1", elapsedAt(40));
        assertEquals(0L, snapshot.getRoundUsageMs());
        assertTrue(snapshot.getFiredCheckpointsMs().isEmpty());
        assertEquals("lock-idle", snapshot.getLock().getCommandId());
        assertTrue(engine.expireIdleRounds(
                elapsedAt(41), wallAt(41)).isEmpty());
    }

    @Test
    public void idleExpirationNeverClearsAnOpenForegroundRound() {
        AppSupervisionEngine engine = engine(30, 5);
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));

        assertTrue(engine.expireIdleRounds(
                elapsedAt(40), wallAt(40)).isEmpty());
        assertEquals(40 * MINUTE_MS,
                engine.snapshot("group-1", elapsedAt(40)).getRoundUsageMs());
    }

    @Test
    public void switchingBetweenAliasesKeepsOneOpenInterval() {
        AppSupervisionEngine engine = engine(30, 20);

        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        engine.onForegroundChanged("com.example.clone", elapsedAt(10), wallAt(10));
        engine.onForegroundChanged("com.example.other", elapsedAt(20), wallAt(20));

        assertEquals(20 * MINUTE_MS, engine.snapshot("group-1", elapsedAt(20)).getRoundUsageMs());
    }

    @Test
    public void directivesClampDurationAndLaterSameKindReplacesDeadline() {
        AppSupervisionEngine engine = engine(30, 20);

        engine.setLock("group-1", 0, "role-main", "休息一下", "cmd-1", at(3, 0));
        assertEquals(MINUTE_MS, engine.snapshot("group-1", elapsedAt(3, 0))
                .getLock().getDurationMs());

        engine.setLock("group-1", 999, "role-main", "稍后再来", "cmd-2", at(3, 1));
        TimedDirective lock = engine.snapshot("group-1", elapsedAt(3, 1)).getLock();
        assertEquals(120 * MINUTE_MS, lock.getDurationMs());
        assertEquals(elapsedAt(3, 121), lock.getDeadlineElapsedMs());
        assertEquals("cmd-2", lock.getCommandId());
    }

    @Test
    public void temporaryUnlockOverridesWithoutDeletingLock() {
        AppSupervisionEngine engine = engine(30, 20);

        engine.setLock("group-1", 60, "role-main", "休息一下", "cmd-1", at(3, 0));
        engine.setTemporaryUnlock(
                "group-1", 10, "role-second", "有正事", "cmd-2", at(3, 40));

        assertEquals(EffectiveState.TEMPORARILY_UNLOCKED,
                engine.effectiveState("group-1", elapsedAt(3, 45)));
        assertEquals(EffectiveState.LOCKED,
                engine.effectiveState("group-1", elapsedAt(3, 51)));
        assertEquals("cmd-1", engine.snapshot("group-1", elapsedAt(3, 51))
                .getLock().getCommandId());
    }

    @Test
    public void disablingFeatureDoesNotClearLockAndActiveLockRejectsGroupDeletion() {
        AppSupervisionEngine engine = engine(30, 20);
        engine.setLock("group-1", 60, "role-main", "休息一下", "cmd-1", at(0));

        engine.setFeatureEnabled(false);
        assertEquals(EffectiveState.LOCKED, engine.effectiveState("group-1", elapsedAt(1)));

        try {
            engine.removeGroup("group-1", elapsedAt(1));
            throw new AssertionError("active lock deletion should fail");
        } catch (IllegalStateException expected) {
            assertEquals("active lock must be removed first", expected.getMessage());
        }

        assertTrue(engine.hasGroup("group-1"));
        assertEquals(EffectiveState.LOCKED, engine.effectiveState("group-1", elapsedAt(1)));
    }

    @Test
    public void deletingUnlockedGroupClearsConfigurationAndRuntimeState() {
        AppSupervisionEngine engine = engine(30, 20);
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));
        engine.onForegroundChanged("com.example.other", elapsedAt(1), wallAt(1));

        engine.removeGroup("group-1", elapsedAt(2));

        assertFalse(engine.hasGroup("group-1"));
        assertFalse(engine.snapshots(elapsedAt(2)).containsKey("group-1"));
        assertEquals(EffectiveState.NORMAL, engine.effectiveState("group-1", elapsedAt(2)));
    }

    @Test
    public void screenOffClosesUsageAndUserPresentDoesNotInventForeground() {
        AppSupervisionEngine engine = engine(30, 20);
        engine.onForegroundChanged("com.example.main", elapsedAt(0), wallAt(0));

        engine.onScreenOff(elapsedAt(7), wallAt(7));
        engine.onUserPresent(elapsedAt(8), wallAt(8));

        AppGroupState.Snapshot snapshot = engine.snapshot("group-1", elapsedAt(9));
        assertEquals(7 * MINUTE_MS, snapshot.getRoundUsageMs());
        assertFalse(snapshot.isForegroundOpen());
    }

    @Test
    public void removeLockOnlyRemovesLockDirective() {
        AppSupervisionEngine engine = engine(30, 20);
        engine.setLock("group-1", 60, "role-main", "休息一下", "cmd-1", at(0));
        engine.setTemporaryUnlock("group-1", 10, "role-second", "有正事", "cmd-2", at(0));

        engine.removeLock("group-1", "cmd-3", at(1));

        assertEquals(EffectiveState.TEMPORARILY_UNLOCKED,
                engine.effectiveState("group-1", elapsedAt(2)));
        assertEquals(null, engine.snapshot("group-1", elapsedAt(2)).getLock());
    }

    private static AppSupervisionEngine engine(int idleMinutes, Integer... checkpointsMinutes) {
        List<Long> checkpoints = new java.util.ArrayList<>();
        for (Integer checkpoint : checkpointsMinutes) {
            checkpoints.add(checkpoint * MINUTE_MS);
        }
        SupervisionPolicy policy = SupervisionPolicy.of(
                idleMinutes * MINUTE_MS, checkpoints, "role-main");
        AppGroup group = AppGroup.create(
                "group-1",
                "示例应用",
                Arrays.asList("com.example.main", "com.example.clone"),
                true,
                policy);
        return new AppSupervisionEngine(true, Collections.singletonList(group));
    }

    private static long checkpointCount(List<EngineEvent> events) {
        long count = 0;
        for (EngineEvent event : events) {
            if (event.getType() == EngineEvent.Type.CHECKPOINT_REACHED) {
                count++;
            }
        }
        return count;
    }

    private static long elapsedAt(int minutes) {
        return minutes * MINUTE_MS;
    }

    private static long elapsedAt(int hours, int minutes) {
        return (hours * 60L + minutes) * MINUTE_MS;
    }

    private static long wallAt(int minutes) {
        return WALL_BASE + elapsedAt(minutes);
    }

    private static SupervisionTime at(int minutes) {
        return new SupervisionTime(elapsedAt(minutes), wallAt(minutes));
    }

    private static SupervisionTime at(int hours, int minutes) {
        long elapsed = elapsedAt(hours, minutes);
        return new SupervisionTime(elapsed, WALL_BASE + elapsed);
    }
}
