package com.aion.chat.homecoming;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class HomecomingContextBuilder {
    private final HomecomingIdentityRepository identities;
    private final MemoryRecall memories;
    private final MessageHistory history;
    private final int characterBudget;
    private final CapabilityContext capabilityContext;

    public HomecomingContextBuilder(
            HomecomingIdentityRepository identities,
            MemoryRecall memories,
            MessageHistory history,
            int characterBudget) {
        this(identities, memories, history, characterBudget, (main, second) -> "");
    }

    public HomecomingContextBuilder(
            HomecomingIdentityRepository identities,
            MemoryRecall memories,
            MessageHistory history,
            int characterBudget,
            CapabilityContext capabilityContext) {
        this.identities = identities;
        this.memories = memories;
        this.history = history;
        if (characterBudget < 500) {
            throw new IllegalArgumentException("character budget is too small");
        }
        this.characterBudget = characterBudget;
        if (capabilityContext == null) {
            throw new IllegalArgumentException("capabilityContext is required");
        }
        this.capabilityContext = capabilityContext;
    }

    public List<HomecomingModelGateway.ChatMessage> build(
            String timelineId, String responderOwner, String trigger,
            long now, String locationState) {
        HomecomingIdentityRepository.Identity identity =
                identities.identityForTimeline(timelineId, responderOwner);
        List<HomecomingMemoryRepository.Memory> recalled =
                memories.recall(identity.ownerId, trigger, 12);
        StringBuilder system = new StringBuilder();
        append(system, identity.systemPrompt);
        append(system, "当前对话角色：" + identity.companionName);
        append(system, "角色设定：" + identity.companionPersona);
        append(system, "用户：" + identity.userName);
        append(system, "用户资料：" + identity.userPersona);
        append(system, "当前时间戳：" + now);
        if (locationState != null && !locationState.trim().isEmpty()) {
            append(system, "当前位置状态：" + locationState.trim());
        }
        append(system, "归巢当前能力：聊天、查看记忆、手动新增编辑删除记忆。");
        try {
            HomecomingIdentityRepository.Identity configuredMain =
                    identities.identityForTimeline("main_private", "main");
            HomecomingIdentityRepository.Identity configuredSecond =
                    identities.identityForTimeline("companion_private", "second");
            append(system, capabilityContext.build(
                    configuredMain.companionName,
                    configuredSecond.companionName));
        } catch (Exception ignored) {
            // Optional local capabilities never block chat context.
        }
        if (!recalled.isEmpty()) {
            append(system, "相关记忆：");
            for (HomecomingMemoryRepository.Memory memory : recalled) {
                append(system, "- " + memory.content);
            }
        }

        ArrayList<HomecomingModelGateway.ChatMessage> recent = new ArrayList<>();
        for (HomecomingChatRepository.Message message :
                history.list(timelineId, Long.MAX_VALUE, 80)) {
            recent.add(new HomecomingModelGateway.ChatMessage(
                    "system".equals(message.role)
                            ? "system"
                            : "assistant".equals(message.role) ? "assistant" : "user",
                    message.text));
        }
        String normalizedTrigger = trigger == null ? "" : trigger.trim();
        int fixed = system.length() + normalizedTrigger.length();
        while (!recent.isEmpty() && fixed + characterCount(recent) > characterBudget) {
            recent.remove(0);
        }
        ArrayList<HomecomingModelGateway.ChatMessage> output = new ArrayList<>();
        output.add(new HomecomingModelGateway.ChatMessage("system", system.toString()));
        output.addAll(recent);
        output.add(new HomecomingModelGateway.ChatMessage("user", normalizedTrigger));
        return Collections.unmodifiableList(output);
    }

    private static int characterCount(List<HomecomingModelGateway.ChatMessage> messages) {
        int result = 0;
        for (HomecomingModelGateway.ChatMessage message : messages) {
            result += message.text.length();
        }
        return result;
    }

    private static void append(StringBuilder value, String line) {
        if (line == null || line.trim().isEmpty()) return;
        if (value.length() > 0) value.append('\n');
        value.append(line.trim());
    }

    interface MemoryRecall {
        List<HomecomingMemoryRepository.Memory> recall(
                String owner, String query, int limit);
    }

    interface MessageHistory {
        List<HomecomingChatRepository.Message> list(
                String timelineId, long beforeCreatedAt, int limit);
    }

    public interface CapabilityContext {
        String build(String mainName, String secondName);
    }
}
