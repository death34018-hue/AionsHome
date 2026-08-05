package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

public class HomecomingScheduleRepositoryTest {
    private static final long NOW = 1_000_000L;
    private static final long FUTURE = NOW + 60_000L;

    @Test
    public void createWritesScheduleAndAppendOnlyOperation() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingScheduleRepository repository = repository(backend);

        HomecomingScheduleRepository.Schedule created = repository.create(
                "alarm", FUTURE, "起床", "main", "main_private", NOW);

        assertEquals("alarm", created.type);
        assertEquals("active", created.status);
        assertEquals(1, repository.listActive().size());
        assertEquals(1, backend.operations.size());
        assertEquals("schedule", backend.operations.get(0).entityType);
        assertEquals("create", backend.operations.get(0).action);
        assertEquals(1L, backend.operations.get(0).deviceSeq);
    }

    @Test
    public void localTombstoneWinsOverSnapshotAndKeepsBaseHash() {
        MemoryBackend backend = new MemoryBackend();
        backend.snapshots.add(schedule(
                "server-schedule", "reminder", FUTURE, "带伞", "main",
                "main_private", "server-hash", "active", NOW - 10L, NOW - 10L, null));
        HomecomingScheduleRepository repository = repository(backend);

        HomecomingScheduleRepository.Schedule deleted =
                repository.delete("server-schedule", NOW);

        assertEquals("deleted", deleted.status);
        assertTrue(repository.listActive().isEmpty());
        assertEquals("server-hash", backend.operations.get(0).baseRevision);
        assertEquals("delete", backend.operations.get(0).action);
    }

    @Test
    public void executionClaimIsAtMostOnceForScheduleAndTrigger() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingScheduleRepository repository = repository(backend);
        HomecomingScheduleRepository.Schedule created = repository.create(
                "monitor", FUTURE, "看看状态", "second", "group", NOW);

        HomecomingScheduleRepository.ExecutionClaim first =
                repository.claimExecution(created.id, FUTURE, NOW);
        HomecomingScheduleRepository.ExecutionClaim duplicate =
                repository.claimExecution(created.id, FUTURE, NOW + 1L);

        assertTrue(first.claimed);
        assertFalse(duplicate.claimed);
        assertEquals(first.executionId, duplicate.executionId);
        assertEquals(1, backend.executions.size());
    }

    @Test
    public void completeExecutionAppendsExecuteOperationOnlyOnce() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingScheduleRepository repository = repository(backend);
        HomecomingScheduleRepository.Schedule created = repository.create(
                "alarm", FUTURE, "喝水", "main", "main_private", NOW);
        HomecomingScheduleRepository.ExecutionClaim claim =
                repository.claimExecution(created.id, FUTURE, NOW);

        repository.completeExecution(claim.executionId, "message-one", NOW + 2L);
        repository.completeExecution(claim.executionId, "message-one", NOW + 3L);

        assertEquals("complete", backend.executions.get(claim.executionId).state);
        assertTrue(repository.listActive().isEmpty());
        assertEquals(2, backend.operations.size());
        assertEquals("execute", backend.operations.get(1).action);
        assertEquals(2L, backend.operations.get(1).deviceSeq);
    }

    @Test
    public void failedOrAbandonedClaimCanRecoverButFreshClaimCannotOverlap() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingScheduleRepository repository = repository(backend);
        HomecomingScheduleRepository.Schedule created = repository.create(
                "alarm", FUTURE, "喝水", "main", "main_private", NOW);

        HomecomingScheduleRepository.ExecutionClaim first =
                repository.claimExecution(created.id, FUTURE, NOW);
        HomecomingScheduleRepository.ExecutionClaim freshDuplicate =
                repository.claimExecution(created.id, FUTURE, NOW + 10L);
        assertTrue(first.claimed);
        assertFalse(freshDuplicate.claimed);

        repository.failExecution(first.executionId, "MODEL_TIMEOUT", NOW + 20L);
        HomecomingScheduleRepository.ExecutionClaim retryAfterFailure =
                repository.claimExecution(created.id, FUTURE, NOW + 30L);
        assertTrue(retryAfterFailure.claimed);

        backend.executions.put(first.executionId,
                new HomecomingScheduleRepository.Execution(
                        first.executionId, created.id, FUTURE, "claimed",
                        "", "", NOW, null));
        HomecomingScheduleRepository.ExecutionClaim retryAfterProcessLoss =
                repository.claimExecution(
                        created.id, FUTURE, NOW + 300_001L);
        assertTrue(retryAfterProcessLoss.claimed);
    }

    @Test
    public void validatesTypeOwnerTimelineAndStaleTime() {
        HomecomingScheduleRepository repository = repository(new MemoryBackend());

        assertThrows(IllegalArgumentException.class, () -> repository.create(
                "music", FUTURE, "x", "main", "main_private", NOW));
        assertThrows(IllegalArgumentException.class, () -> repository.create(
                "alarm", FUTURE, "x", "third", "main_private", NOW));
        assertThrows(IllegalArgumentException.class, () -> repository.create(
                "alarm", FUTURE, "x", "main", "unknown", NOW));
        assertThrows(IllegalArgumentException.class, () -> repository.create(
                "alarm", NOW - 90_001L, "x", "main", "main_private", NOW));
    }

    @Test
    public void failedExecutionKeepsScheduleAndSanitizesDiagnostic() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingScheduleRepository repository = repository(backend);
        HomecomingScheduleRepository.Schedule created = repository.create(
                "reminder", FUTURE, "带伞", "second", "companion_private", NOW);
        HomecomingScheduleRepository.ExecutionClaim claim =
                repository.claimExecution(created.id, FUTURE, NOW);

        repository.failExecution(
                claim.executionId, "MODEL_TIMEOUT\nsecret detail", NOW + 2L);

        assertEquals("failed", backend.executions.get(claim.executionId).state);
        assertEquals("MODEL_TIMEOUT secret detail",
                backend.executions.get(claim.executionId).diagnostic);
        assertNotNull(repository.find(created.id));
    }

    private static HomecomingScheduleRepository repository(MemoryBackend backend) {
        return new HomecomingScheduleRepository(
                backend, "epoch-one", "android:test-device");
    }

    private static HomecomingScheduleRepository.Schedule schedule(
            String id, String type, long triggerAt, String content,
            String ownerId, String timelineId, String baseHash, String status,
            long createdAt, long updatedAt, Long endedAt) {
        return new HomecomingScheduleRepository.Schedule(
                id, type, triggerAt, content, ownerId, timelineId,
                baseHash, status, createdAt, updatedAt, endedAt);
    }

    private static final class MemoryBackend
            implements HomecomingScheduleRepository.Backend {
        final List<HomecomingScheduleRepository.Schedule> snapshots = new ArrayList<>();
        final Map<String, HomecomingScheduleRepository.Schedule> locals =
                new LinkedHashMap<>();
        final Map<String, HomecomingScheduleRepository.Execution> executions =
                new LinkedHashMap<>();
        final List<HomecomingOperationJournal.Operation> operations = new ArrayList<>();
        long sequence;

        @Override
        public List<HomecomingScheduleRepository.Schedule> snapshotSchedules() {
            return new ArrayList<>(snapshots);
        }

        @Override
        public List<HomecomingScheduleRepository.Schedule> localSchedules() {
            return new ArrayList<>(locals.values());
        }

        @Override
        public HomecomingScheduleRepository.Schedule writeLocal(
                HomecomingScheduleRepository.Schedule schedule,
                HomecomingOperationJournal.Operation operation) {
            locals.put(schedule.id, schedule);
            operations.add(operation.withDeviceSeq(++sequence));
            return schedule;
        }

        @Override
        public HomecomingScheduleRepository.ExecutionClaim claim(
                String executionId, String scheduleId, long triggerAt, long now) {
            HomecomingScheduleRepository.Execution existing = executions.get(executionId);
            if (existing != null) {
                if ("failed".equals(existing.state)
                        || ("claimed".equals(existing.state)
                        && now - existing.startedAt > 300_000L)) {
                    HomecomingScheduleRepository.Execution recovered =
                            new HomecomingScheduleRepository.Execution(
                                    executionId, scheduleId, triggerAt, "claimed",
                                    "", "", now, null);
                    executions.put(executionId, recovered);
                    return new HomecomingScheduleRepository.ExecutionClaim(
                            executionId, scheduleId, triggerAt, true, "claimed");
                }
                return new HomecomingScheduleRepository.ExecutionClaim(
                        executionId, scheduleId, triggerAt, false, existing.state);
            }
            HomecomingScheduleRepository.Execution created =
                    new HomecomingScheduleRepository.Execution(
                            executionId, scheduleId, triggerAt, "claimed",
                            "", "", now, null);
            executions.put(executionId, created);
            return new HomecomingScheduleRepository.ExecutionClaim(
                    executionId, scheduleId, triggerAt, true, "claimed");
        }

        @Override
        public HomecomingScheduleRepository.Execution execution(String executionId) {
            return executions.get(executionId);
        }

        @Override
        public void complete(
                String executionId, String messageId, long now,
                HomecomingScheduleRepository.Schedule completedSchedule,
                HomecomingOperationJournal.Operation operation) {
            HomecomingScheduleRepository.Execution current = executions.get(executionId);
            if (current == null) throw new IllegalStateException("unknown execution");
            if ("complete".equals(current.state)) return;
            executions.put(executionId, new HomecomingScheduleRepository.Execution(
                    executionId, current.scheduleId, current.triggerAt, "complete",
                    messageId, "", current.startedAt, now));
            locals.put(current.scheduleId, completedSchedule);
            operations.add(operation.withDeviceSeq(++sequence));
        }

        @Override
        public void fail(String executionId, String diagnostic, long now) {
            HomecomingScheduleRepository.Execution current = executions.get(executionId);
            if (current == null) throw new IllegalStateException("unknown execution");
            executions.put(executionId, new HomecomingScheduleRepository.Execution(
                    executionId, current.scheduleId, current.triggerAt, "failed",
                    "", diagnostic, current.startedAt, now));
        }
    }
}
