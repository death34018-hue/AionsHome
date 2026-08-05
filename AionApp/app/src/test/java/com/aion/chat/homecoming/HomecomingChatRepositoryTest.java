package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

public class HomecomingChatRepositoryTest {
    @Test
    public void duplicateRequestCannotDuplicateEitherMessageRole() throws Exception {
        MemoryBackend backend = new MemoryBackend();
        HomecomingChatRepository repository = new HomecomingChatRepository(
                backend, "epoch-one", "android:test-device");

        HomecomingChatRepository.Message first = repository.commitUserMessage(
                "request-one", "main_private", "user", "hello", "", 10L);
        HomecomingChatRepository.Message duplicate = repository.commitUserMessage(
                "request-one", "main_private", "user", "hello", "", 11L);
        repository.commitAssistantMessage(
                "request-one", "main_private", "role-main", "complete answer", 20L);
        repository.commitAssistantMessage(
                "request-one", "main_private", "role-main", "complete answer", 21L);

        assertEquals(first.id, duplicate.id);
        assertEquals("epoch-one", first.epochId);
        assertEquals("epoch-one",
                new org.json.JSONObject(backend.operations.get(0).payloadJson)
                        .getString("epoch_id"));
        assertEquals(2, backend.messages.size());
        assertEquals(2, backend.operations.size());
        assertEquals(1L, backend.operations.get(0).deviceSeq);
        assertEquals(2L, backend.operations.get(1).deviceSeq);
    }

    @Test
    public void failedRequestCannotCommitPartialAssistantText() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingChatRepository repository = new HomecomingChatRepository(
                backend, "epoch-one", "android:test-device");
        repository.commitUserMessage(
                "request-two", "group", "user", "question", "", 10L);

        repository.failRequest("request-two", "MODEL_TIMEOUT");

        assertThrows(IllegalStateException.class, () ->
                repository.commitAssistantMessage(
                        "request-two", "group", "role-main", "partial", 20L));
        assertEquals(1, backend.messages.size());
        assertEquals("failed", backend.states.get("request-two"));
    }

    @Test
    public void timelineListingIsChronologicalAndBounded() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingChatRepository repository = new HomecomingChatRepository(
                backend, "epoch-one", "android:test-device");
        for (int i = 0; i < 4; i++) {
            repository.commitUserMessage(
                    "request-" + i, "main_private", "user", "m" + i, "", 10L + i);
        }

        List<HomecomingChatRepository.Message> rows =
                repository.listMessages("main_private", 13L, 2);

        assertEquals(2, rows.size());
        assertEquals("m1", rows.get(0).text);
        assertEquals("m2", rows.get(1).text);
    }

    @Test
    public void timelineListingNeverIncludesMessagesFromAnOlderEpoch() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingChatRepository oldEpoch = new HomecomingChatRepository(
                backend, "epoch-old", "android:test-device");
        HomecomingChatRepository currentEpoch = new HomecomingChatRepository(
                backend, "epoch-current", "android:test-device");
        oldEpoch.commitUserMessage(
                "request-old", "group", "user", "old emergency", "", 99L);
        currentEpoch.commitUserMessage(
                "request-current", "group", "user", "current emergency", "", 10L);

        List<HomecomingChatRepository.Message> rows =
                currentEpoch.listMessages("group", 100L, 20);

        assertEquals(1, rows.size());
        assertEquals("current emergency", rows.get(0).text);
    }

    @Test
    public void supervisionSystemResultIsIdempotentAndKeepsSystemRole() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingChatRepository repository = new HomecomingChatRepository(
                backend, "epoch-one", "android:test-device");

        HomecomingChatRepository.Message first = repository.commitSystemMessage(
                "request-one:supervision:0",
                "main_private",
                "【配置伴侣名】已锁定短视频 20 分钟",
                30L);
        HomecomingChatRepository.Message duplicate = repository.commitSystemMessage(
                "request-one:supervision:0",
                "main_private",
                "【配置伴侣名】已锁定短视频 20 分钟",
                31L);

        assertEquals(first.id, duplicate.id);
        assertEquals("system", first.role);
        assertEquals("system", first.senderId);
        assertEquals(1, backend.messages.size());
        assertEquals(1, backend.operations.size());
    }

    private static final class MemoryBackend implements HomecomingChatRepository.Backend {
        final Map<String, HomecomingChatRepository.Message> messages = new LinkedHashMap<>();
        final Map<String, String> states = new LinkedHashMap<>();
        final List<HomecomingOperationJournal.Operation> operations = new ArrayList<>();
        long sequence;

        @Override
        public synchronized HomecomingChatRepository.Message commit(
                HomecomingChatRepository.Message candidate,
                HomecomingOperationJournal.Operation operation,
                boolean assistant) {
            String key = candidate.requestId + ":" + candidate.role;
            HomecomingChatRepository.Message existing = messages.get(key);
            if (existing != null) {
                return existing;
            }
            String state = states.get(candidate.requestId);
            if (assistant && "failed".equals(state)) {
                throw new IllegalStateException("request already failed");
            }
            messages.put(key, candidate);
            states.put(candidate.requestId, assistant ? "complete" : "pending");
            operations.add(operation.withDeviceSeq(++sequence));
            return candidate;
        }

        @Override
        public void fail(String requestId, String code) {
            states.put(requestId, "failed");
        }

        @Override
        public List<HomecomingChatRepository.Message> list(
                String epochId, String timelineId, long beforeCreatedAt, int limit) {
            ArrayList<HomecomingChatRepository.Message> result = new ArrayList<>();
            for (HomecomingChatRepository.Message message : messages.values()) {
                if (epochId.equals(message.epochId)
                        && timelineId.equals(message.timelineId)
                        && message.createdAt < beforeCreatedAt) {
                    result.add(message);
                }
            }
            result.sort((left, right) -> Long.compare(left.createdAt, right.createdAt));
            while (result.size() > limit) {
                result.remove(0);
            }
            return result;
        }
    }
}
