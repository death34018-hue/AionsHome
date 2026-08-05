package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionRepositoryTest {
    private static final long NOW = 1_000_000L;

    @Test
    public void enqueueIsIdempotentAndStartsPending() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingSupervisionRepository repository = repository(backend);

        HomecomingSupervisionRepository.Event first = repository.enqueue(
                "event-one", "group-one", 20 * 60_000L, "role-main",
                "{\"eventType\":\"checkpoint\"}", NOW);
        HomecomingSupervisionRepository.Event duplicate = repository.enqueue(
                "event-one", "group-one", 20 * 60_000L, "role-main",
                "{\"eventType\":\"checkpoint\"}", NOW + 1L);

        assertEquals("pending", first.state);
        assertEquals(first.eventId, duplicate.eventId);
        assertEquals(1, backend.events.size());
        assertEquals(1, repository.recoverable(NOW).size());
    }

    @Test
    public void claimRejectsFreshDuplicateButRecoversStaleRunningEvent() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingSupervisionRepository repository = repository(backend);
        repository.enqueue(
                "event-one", "group-one", 20 * 60_000L, "role-main",
                "{}", NOW);

        HomecomingSupervisionRepository.Claim first =
                repository.claim("event-one", NOW);
        HomecomingSupervisionRepository.Claim duplicate =
                repository.claim("event-one", NOW + 1L);
        HomecomingSupervisionRepository.Claim stale =
                repository.claim("event-one", NOW + 300_001L);

        assertTrue(first.claimed);
        assertFalse(duplicate.claimed);
        assertTrue(stale.claimed);
        assertEquals(2, stale.attemptCount);
    }

    @Test
    public void failureCanRetryAndCompletionAppendsOneOperation() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingSupervisionRepository repository = repository(backend);
        repository.enqueue(
                "event-one", "group-one", 20 * 60_000L, "role-main",
                "{}", NOW);
        repository.claim("event-one", NOW);
        repository.fail("event-one", "MODEL_TIMEOUT\nprivate detail", NOW + 2L);

        HomecomingSupervisionRepository.Claim retry =
                repository.claim("event-one", NOW + 3L);
        assertTrue(retry.claimed);
        repository.complete(
                "event-one", "message-one", "checkpoint complete", NOW + 4L);
        repository.complete(
                "event-one", "message-one", "checkpoint complete", NOW + 5L);

        HomecomingSupervisionRepository.Event completed =
                backend.events.get("event-one");
        assertEquals("complete", completed.state);
        assertEquals("message-one", completed.messageId);
        assertEquals(1, backend.operations.size());
        assertEquals("supervision_event", backend.operations.get(0).entityType);
        assertEquals("execute", backend.operations.get(0).action);
    }

    @Test
    public void recoveryNeverCrossesHomecomingEpochs() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingSupervisionRepository repository = repository(backend);
        backend.events.put("old-event", new HomecomingSupervisionRepository.Event(
                "old-event", "old-epoch", "group-one", 600_000L, "aion",
                "{}", "pending", 0, "", "", "", NOW, NOW));

        assertTrue(repository.recoverable(NOW).isEmpty());
        assertNull(repository.find("old-event"));
    }

    private static HomecomingSupervisionRepository repository(MemoryBackend backend) {
        return new HomecomingSupervisionRepository(
                backend, "epoch-one", "android:test-device");
    }

    private static final class MemoryBackend
            implements HomecomingSupervisionRepository.Backend {
        final Map<String, HomecomingSupervisionRepository.Event> events =
                new LinkedHashMap<>();
        final List<HomecomingOperationJournal.Operation> operations =
                new ArrayList<>();

        @Override
        public HomecomingSupervisionRepository.Event putIfAbsent(
                HomecomingSupervisionRepository.Event event) {
            HomecomingSupervisionRepository.Event existing = events.get(event.eventId);
            if (existing != null) return existing;
            events.put(event.eventId, event);
            return event;
        }

        @Override
        public HomecomingSupervisionRepository.Event find(String eventId) {
            return events.get(eventId);
        }

        @Override
        public List<HomecomingSupervisionRepository.Event> recoverable(long staleBefore) {
            ArrayList<HomecomingSupervisionRepository.Event> values = new ArrayList<>();
            for (HomecomingSupervisionRepository.Event event : events.values()) {
                if ("pending".equals(event.state) || "failed".equals(event.state)
                        || ("running".equals(event.state)
                        && event.updatedAt < staleBefore)) {
                    values.add(event);
                }
            }
            return values;
        }

        @Override
        public HomecomingSupervisionRepository.Claim claim(
                String eventId, long now, long staleBefore) {
            HomecomingSupervisionRepository.Event current = events.get(eventId);
            if (current == null) {
                return new HomecomingSupervisionRepository.Claim(
                        eventId, false, "missing", 0);
            }
            boolean allowed = "pending".equals(current.state)
                    || "failed".equals(current.state)
                    || ("running".equals(current.state)
                    && current.updatedAt < staleBefore);
            if (!allowed) {
                return new HomecomingSupervisionRepository.Claim(
                        eventId, false, current.state, current.attemptCount);
            }
            HomecomingSupervisionRepository.Event running = current.withState(
                    "running", current.attemptCount + 1, "", "", "", now);
            events.put(eventId, running);
            return new HomecomingSupervisionRepository.Claim(
                    eventId, true, "running", running.attemptCount);
        }

        @Override
        public void complete(
                String eventId,
                String messageId,
                String resultText,
                long now,
                HomecomingOperationJournal.Operation operation) {
            HomecomingSupervisionRepository.Event current = events.get(eventId);
            if ("complete".equals(current.state)) return;
            events.put(eventId, current.withState(
                    "complete", current.attemptCount, "", messageId, resultText, now));
            operations.add(operation);
        }

        @Override
        public void fail(String eventId, String diagnostic, long now) {
            HomecomingSupervisionRepository.Event current = events.get(eventId);
            events.put(eventId, current.withState(
                    "failed", current.attemptCount, diagnostic, "", "", now));
        }
    }
}
