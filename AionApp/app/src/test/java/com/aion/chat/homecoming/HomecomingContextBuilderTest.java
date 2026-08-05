package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingContextBuilderTest {
    @Test
    public void privateTimelinesUseConfiguredIdentityAndIsolatedMemoryOwner() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"Configured User\",\"persona\":\"User Persona\"},"
                        + "\"companions\":{\"main\":{\"name\":\"Configured Main\","
                        + "\"persona\":\"Main Persona\"},\"second\":{"
                        + "\"name\":\"Configured Second\",\"persona\":\"Second Persona\"}},"
                        + "\"system_prompt\":\"Configured System\"}");
        RecordingRecall recall = new RecordingRecall();
        HomecomingContextBuilder builder = new HomecomingContextBuilder(
                identities, recall, (timeline, before, limit) -> Collections.emptyList(), 4000);

        List<HomecomingModelGateway.ChatMessage> main =
                builder.build("main_private", "main", "hello", 100L, "home");
        List<HomecomingModelGateway.ChatMessage> second =
                builder.build("companion_private", "second", "hello", 100L, "home");

        assertTrue(main.get(0).text.contains("Configured Main"));
        assertTrue(main.get(0).text.contains("Main Persona"));
        assertTrue(second.get(0).text.contains("Configured Second"));
        assertTrue(second.get(0).text.contains("Second Persona"));
        assertEquals(Arrays.asList("main", "second"), recall.owners);
    }

    @Test
    public void groupBuildCanUseEitherResponderWithoutCrossingMemory() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"U\",\"persona\":\"UP\"},\"companions\":{"
                        + "\"main\":{\"name\":\"M\",\"persona\":\"MP\"},"
                        + "\"second\":{\"name\":\"S\",\"persona\":\"SP\"}}}");
        RecordingRecall recall = new RecordingRecall();
        HomecomingContextBuilder builder = new HomecomingContextBuilder(
                identities, recall, (timeline, before, limit) -> Collections.emptyList(), 4000);

        assertTrue(builder.build("group", "main", "q", 10L, "").get(0).text.contains("M"));
        assertTrue(builder.build("group", "second", "q", 10L, "").get(0).text.contains("S"));
        assertEquals(Arrays.asList("main", "second"), recall.owners);
    }

    @Test
    public void contextContainsNoExcludedAutonomyOrEntertainmentCapabilities() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"U\",\"persona\":\"UP\"},\"companions\":{"
                        + "\"main\":{\"name\":\"M\",\"persona\":\"MP\"},"
                        + "\"second\":{\"name\":\"S\",\"persona\":\"SP\"}}}");
        HomecomingContextBuilder builder = new HomecomingContextBuilder(
                identities, new RecordingRecall(),
                (timeline, before, limit) -> Collections.emptyList(), 4000);

        String serialized = builder.build(
                "main_private", "main", "hello", 10L, "").toString();

        assertFalse(serialized.contains("空闲自主"));
        assertFalse(serialized.contains("朋友圈"));
        assertFalse(serialized.contains("娱乐"));
        assertFalse(serialized.contains("压缩"));
    }

    @Test
    public void configuredSupervisionContextIsIncludedWithoutChangingIdentityRouting() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"Configured User\",\"persona\":\"UP\"},"
                        + "\"companions\":{\"main\":{\"name\":\"Configured Main\","
                        + "\"persona\":\"MP\"},\"second\":{\"name\":\"Configured Second\","
                        + "\"persona\":\"SP\"}}}");
        HomecomingContextBuilder builder = new HomecomingContextBuilder(
                identities,
                new RecordingRecall(),
                (timeline, before, limit) -> Collections.emptyList(),
                4000,
                (mainName, secondName) ->
                        "监督角色：" + mainName + " / " + secondName);

        String main = builder.build(
                "main_private", "main", "hello", 10L, "").get(0).text;

        assertTrue(main.contains("监督角色：Configured Main / Configured Second"));
        assertTrue(main.contains("当前对话角色：Configured Main"));
    }

    @Test
    public void supervisionContextFailureDoesNotBreakNormalChatContext() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"U\",\"persona\":\"UP\"},\"companions\":{"
                        + "\"main\":{\"name\":\"M\",\"persona\":\"MP\"},"
                        + "\"second\":{\"name\":\"S\",\"persona\":\"SP\"}}}");
        HomecomingContextBuilder builder = new HomecomingContextBuilder(
                identities,
                new RecordingRecall(),
                (timeline, before, limit) -> Collections.emptyList(),
                4000,
                (mainName, secondName) -> {
                    throw new IllegalStateException("supervision unavailable");
                });

        String context = builder.build(
                "main_private", "main", "hello", 10L, "").get(0).text;

        assertTrue(context.contains("当前对话角色：M"));
        assertFalse(context.contains("APP_LOCK"));
    }

    @Test
    public void supervisionResultRemainsASystemMessageInLaterContext() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"U\",\"persona\":\"UP\"},\"companions\":{"
                        + "\"main\":{\"name\":\"M\",\"persona\":\"MP\"},"
                        + "\"second\":{\"name\":\"S\",\"persona\":\"SP\"}}}");
        HomecomingChatRepository.Message result = new HomecomingChatRepository.Message(
                "message-one",
                "result-one",
                "main_private",
                "system",
                "system",
                "【M】已锁定短视频 20 分钟",
                "",
                "",
                9L,
                "committed");
        HomecomingContextBuilder builder = new HomecomingContextBuilder(
                identities,
                new RecordingRecall(),
                (timeline, before, limit) -> Collections.singletonList(result),
                4000);

        List<HomecomingModelGateway.ChatMessage> messages = builder.build(
                "main_private", "main", "hello", 10L, "");

        assertEquals("system", messages.get(1).role);
        assertTrue(messages.get(1).text.contains("已锁定"));
    }

    private static final class RecordingRecall implements HomecomingContextBuilder.MemoryRecall {
        final java.util.ArrayList<String> owners = new java.util.ArrayList<>();
        @Override
        public List<HomecomingMemoryRepository.Memory> recall(
                String owner, String query, int limit) {
            owners.add(owner);
            return Collections.singletonList(new HomecomingMemoryRepository.Memory(
                    owner + "-memory", owner, owner + " memory", owner, "", false, 1L));
        }
    }
}
