package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingMemorySummarizerTest {
    @Test
    public void minimumAndIdleGatesDoNotCallTheModelOrAdvanceTheAnchor() {
        Fixture tooFew = new Fixture(messages("main_private", 3, 1_000L));
        RecordingCompletion first = new RecordingCompletion();
        tooFew.summarizer(4, 50, 0).summarizeOwner("main", 10_000L, first);
        assertEquals("minimum_not_met", first.result.status);
        assertEquals(0, tooFew.model.groupSizes.size());
        assertEquals("", tooFew.store.anchor("main"));

        Fixture notIdle = new Fixture(messages("main_private", 4, 9_500L));
        RecordingCompletion second = new RecordingCompletion();
        notIdle.summarizer(4, 50, 1_000L).summarizeOwner("main", 10_000L, second);
        assertEquals("idle_not_met", second.result.status);
        assertEquals(0, notIdle.model.groupSizes.size());
        assertEquals("", notIdle.store.anchor("main"));
    }

    @Test
    public void balancedGroupsCommitAtomicallyAndRestartAfterTheLastCommittedGroup() {
        Fixture fixture = new Fixture(messages("main_private", 60, 1_000L));
        fixture.model.responses.add(memoryJson("memory one", "m-0"));
        fixture.model.responses.add("{broken");

        RecordingCompletion first = new RecordingCompletion();
        fixture.summarizer(20, 50, 0)
                .summarizeOwner("main", 100_000L, first);

        assertEquals(java.util.Arrays.asList(30, 30), fixture.model.groupSizes);
        assertEquals("summary_failed", first.result.status);
        assertEquals("m-29", fixture.store.anchor("main"));
        assertEquals(1, fixture.store.memories.size());

        fixture.model.responses.add(memoryJson("memory two", "m-30"));
        RecordingCompletion retry = new RecordingCompletion();
        fixture.summarizer(20, 50, 0)
                .summarizeOwner("main", 100_000L, retry);

        assertEquals("complete", retry.result.status);
        assertEquals("m-59", fixture.store.anchor("main"));
        assertEquals(2, fixture.store.memories.size());
    }

    @Test
    public void ownerIsolationIncludesOnlyItsPrivateTimelineAndSharedGroupEvidence() {
        List<HomecomingMemorySummarizer.SourceMessage> all = new ArrayList<>();
        all.add(message("main-1", "main_private", 1_000L));
        all.add(message("second-1", "companion_private", 2_000L));
        all.add(message("group-1", "group", 3_000L));
        Fixture fixture = new Fixture(all);
        fixture.model.responses.add(memoryJson("second memory", "group-1"));

        RecordingCompletion completion = new RecordingCompletion();
        fixture.summarizer(2, 50, 0)
                .summarizeOwner("second", 10_000L, completion);

        assertEquals("complete", completion.result.status);
        assertEquals(java.util.Arrays.asList("second-1", "group-1"),
                fixture.model.messageIds.get(0));
        assertEquals("second", fixture.store.memories.get(0).ownerId);
    }

    @Test
    public void invalidEvidenceIsDroppedAndEmbeddingFailureKeepsTheTextMemory() {
        Fixture fixture = new Fixture(messages("main_private", 2, 1_000L));
        fixture.model.responses.add("{\"memories\":[{"
                + "\"content\":\"2026-07-27，记住这件事\","
                + "\"type\":\"daily\",\"keywords\":[\"项目\"],"
                + "\"importance\":0.5,"
                + "\"source_message_ids\":[\"m-0\",\"invented\"]}]}");
        fixture.embedding.fail = true;

        RecordingCompletion completion = new RecordingCompletion();
        fixture.summarizer(2, 50, 0)
                .summarizeOwner("main", 10_000L, completion);

        HomecomingMemorySummarizer.MemoryDraft saved = fixture.store.memories.get(0);
        assertEquals(Collections.singletonList("m-0"), saved.sourceMessageIds);
        assertEquals(0, saved.embedding.length);
        assertEquals("complete", completion.result.status);
    }

    @Test
    public void promptUsesConfiguredIdentityAndRequestsOnlyAtomicMemoryJson() {
        String prompt = HomecomingMemorySummarizer.buildPrompt(
                "伴侣甲", "用户乙", "配置的人设",
                messages("main_private", 2, 1_000L));

        assertTrue(prompt.contains("伴侣甲"));
        assertTrue(prompt.contains("用户乙"));
        assertTrue(prompt.contains("配置的人设"));
        assertTrue(prompt.contains("\"memories\""));
        assertTrue(prompt.contains("source_message_ids"));
        assertFalse(prompt.contains("日记"));
        assertFalse(prompt.contains("朋友圈"));
        assertFalse(prompt.contains("人格演化"));
    }

    @Test
    public void automaticSummaryOperationsAreDistinctFromManualMemoryCrud() {
        HomecomingOperationJournal.Operation operation =
                HomecomingMemorySummarizer.automaticMemoryOperation(
                        "epoch-one", "device-one", "memory-one",
                        "{}", 1_000L);

        assertEquals("memory_auto", operation.entityType);
        assertEquals("create", operation.action);
    }

    @Test
    public void validEmptyMemoryResultAdvancesTheAnchor() {
        Fixture fixture = new Fixture(messages("main_private", 1, 1_000L));
        fixture.model.responses.add("{\"memories\":[]}");

        RecordingCompletion completion = new RecordingCompletion();
        fixture.summarizer(1, 50, 0)
                .summarizeOwner("main", 10_000L, completion);

        assertEquals("complete", completion.result.status);
        assertEquals(1, completion.result.processedMessages);
        assertEquals(0, completion.result.createdMemories);
        assertEquals("m-0", fixture.store.anchor("main"));
    }

    private static String memoryJson(String content, String sourceId) {
        return "{\"memories\":[{\"content\":\"" + content + "\","
                + "\"type\":\"daily\",\"keywords\":[\"keyword\"],"
                + "\"importance\":0.4,\"source_message_ids\":[\""
                + sourceId + "\"]}]}";
    }

    private static List<HomecomingMemorySummarizer.SourceMessage> messages(
            String timeline, int count, long firstCreatedAt) {
        ArrayList<HomecomingMemorySummarizer.SourceMessage> values = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            values.add(message("m-" + i, timeline, firstCreatedAt + i));
        }
        return values;
    }

    private static HomecomingMemorySummarizer.SourceMessage message(
            String id, String timeline, long createdAt) {
        return new HomecomingMemorySummarizer.SourceMessage(
                id, timeline, "user", "text " + id, createdAt);
    }

    private static final class Fixture {
        final FilteringSource source;
        final RecordingModel model = new RecordingModel();
        final RecordingStore store = new RecordingStore();
        final FakeEmbedding embedding = new FakeEmbedding();

        Fixture(List<HomecomingMemorySummarizer.SourceMessage> messages) {
            source = new FilteringSource(messages, store);
        }

        HomecomingMemorySummarizer summarizer(
                int minimum, int maximum, long idleMillis) {
            return new HomecomingMemorySummarizer(
                    source, model, store, embedding, minimum, maximum, idleMillis);
        }
    }

    private static final class FilteringSource
            implements HomecomingMemorySummarizer.MessageSource {
        final List<HomecomingMemorySummarizer.SourceMessage> all;
        final RecordingStore store;

        FilteringSource(List<HomecomingMemorySummarizer.SourceMessage> all,
                RecordingStore store) {
            this.all = all;
            this.store = store;
        }

        @Override public List<HomecomingMemorySummarizer.SourceMessage> load(
                String ownerId, String afterMessageId) {
            ArrayList<HomecomingMemorySummarizer.SourceMessage> result = new ArrayList<>();
            boolean after = afterMessageId == null || afterMessageId.isEmpty();
            for (HomecomingMemorySummarizer.SourceMessage message : all) {
                if (!after) {
                    after = message.id.equals(afterMessageId);
                    continue;
                }
                boolean allowed = "group".equals(message.timelineId)
                        || ("main".equals(ownerId)
                        && "main_private".equals(message.timelineId))
                        || ("second".equals(ownerId)
                        && "companion_private".equals(message.timelineId));
                if (allowed) result.add(message);
            }
            return result;
        }
    }

    private static final class RecordingModel
            implements HomecomingMemorySummarizer.SummaryModel {
        final List<String> responses = new ArrayList<>();
        final List<Integer> groupSizes = new ArrayList<>();
        final List<List<String>> messageIds = new ArrayList<>();
        int next;

        @Override public String summarize(
                String ownerId,
                List<HomecomingMemorySummarizer.SourceMessage> messages) {
            groupSizes.add(messages.size());
            ArrayList<String> ids = new ArrayList<>();
            for (HomecomingMemorySummarizer.SourceMessage message : messages) {
                ids.add(message.id);
            }
            messageIds.add(ids);
            return responses.get(next++);
        }
    }

    private static final class RecordingStore
            implements HomecomingMemorySummarizer.MemoryStore {
        final Map<String, String> anchors = new HashMap<>();
        final List<HomecomingMemorySummarizer.MemoryDraft> memories =
                new ArrayList<>();

        @Override public String anchor(String ownerId) {
            String value = anchors.get(ownerId);
            return value == null ? "" : value;
        }

        @Override public void commitGroup(
                String ownerId, String lastMessageId,
                List<String> sourceMessageIds,
                List<HomecomingMemorySummarizer.MemoryDraft> drafts, long now) {
            for (HomecomingMemorySummarizer.MemoryDraft draft : drafts) {
                assertEquals(ownerId, draft.ownerId);
            }
            memories.addAll(drafts);
            anchors.put(ownerId, lastMessageId);
        }
    }

    private static final class FakeEmbedding
            implements HomecomingMemorySummarizer.EmbeddingProvider {
        boolean fail;
        @Override public byte[] embed(String content) throws Exception {
            if (fail) throw new Exception("offline");
            return new byte[]{1, 2, 3};
        }
    }

    private static final class RecordingCompletion
            implements HomecomingMemorySummarizer.Completion {
        HomecomingMemorySummarizer.Result result;
        @Override public void onComplete(HomecomingMemorySummarizer.Result value) {
            result = value;
        }
    }
}
