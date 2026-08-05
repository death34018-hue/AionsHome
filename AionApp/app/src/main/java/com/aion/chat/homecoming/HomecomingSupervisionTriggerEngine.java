package com.aion.chat.homecoming;

import java.util.List;

public final class HomecomingSupervisionTriggerEngine {
    private final EventPort events;
    private final ContextPort context;
    private final GatewayPort gateway;
    private final MessagePort messages;
    private final ControlPort controls;
    private final Clock clock;

    HomecomingSupervisionTriggerEngine(
            EventPort events,
            ContextPort context,
            GatewayPort gateway,
            MessagePort messages,
            ControlPort controls,
            Clock clock) {
        this.events = events;
        this.context = context;
        this.gateway = gateway;
        this.messages = messages;
        this.controls = controls;
        this.clock = clock;
    }

    public void execute(
            String eventId,
            String routeId,
            String modelId,
            Completion completion) {
        HomecomingSupervisionRepository.Event event = events.find(eventId);
        if (event == null) {
            completion.onFailure("unknown_supervision_event");
            return;
        }
        long now = clock.now();
        HomecomingSupervisionRepository.Claim claim = events.claim(eventId, now);
        if (!claim.claimed) {
            completion.onDuplicate();
            return;
        }
        RoleTarget target = target(event.roleId);
        if (target == null) {
            fail(eventId, "unknown_supervision_role", completion);
            return;
        }
        final List<HomecomingModelGateway.ChatMessage> contextMessages;
        try {
            contextMessages = context.build(
                    event,
                    target.ownerId,
                    target.timelineId,
                    trigger(event),
                    now);
        } catch (RuntimeException exception) {
            fail(eventId, "context_failed", completion);
            return;
        }
        try {
            gateway.stream(
                    "supervision:" + eventId,
                    routeId,
                    modelId,
                    contextMessages,
                    new HomecomingModelGateway.StreamObserver() {
                        @Override public void onChunk(String text) {
                            // Background work only publishes a complete response.
                        }

                        @Override public void onComplete(String text) {
                            HomecomingControlParser.Result parsed =
                                    HomecomingControlParser.parse(text);
                            if (parsed.visibleText.isEmpty()) {
                                fail(eventId, "empty_model_reply", completion);
                                return;
                            }
                            try {
                                long completedAt = clock.now();
                                String messageId = messages.commit(
                                        event,
                                        target.ownerId,
                                        target.timelineId,
                                        parsed.visibleText,
                                        completedAt);
                                controls.apply(
                                        event,
                                        target.ownerId,
                                        target.timelineId,
                                        parsed.events,
                                        completedAt);
                                events.complete(
                                        eventId,
                                        messageId,
                                        parsed.visibleText,
                                        completedAt);
                                completion.onComplete(
                                        messageId, parsed.visibleText);
                            } catch (RuntimeException exception) {
                                fail(eventId, "supervision_commit_failed", completion);
                            }
                        }

                        @Override public void onFailure(String code) {
                            fail(
                                    eventId,
                                    code == null || code.trim().isEmpty()
                                            ? "model_request_failed" : code,
                                    completion);
                        }
                    });
        } catch (RuntimeException exception) {
            fail(eventId, "model_request_failed", completion);
        }
    }

    private void fail(String eventId, String code, Completion completion) {
        events.fail(eventId, code, clock.now());
        completion.onFailure(code);
    }

    private static RoleTarget target(String roleId) {
        String stable = roleId == null ? "" : roleId.trim().toLowerCase();
        if ("aion".equals(stable)) {
            return new RoleTarget("main", "main_private");
        }
        if ("connor".equals(stable)) {
            return new RoleTarget("second", "companion_private");
        }
        return null;
    }

    private static String trigger(HomecomingSupervisionRepository.Event event) {
        long minutes = event.checkpointMs / 60_000L;
        return "[监督检查点]\n"
                + "监督分组：" + event.groupId + "\n"
                + "已达到检查点：" + minutes + " 分钟\n"
                + "请依据当前监督状态、近期对话和记忆自然地检查并回应用户。";
    }

    interface EventPort {
        HomecomingSupervisionRepository.Event find(String eventId);
        HomecomingSupervisionRepository.Claim claim(String eventId, long now);
        void complete(String eventId, String messageId, String resultText, long now);
        void fail(String eventId, String diagnostic, long now);
    }

    interface ContextPort {
        List<HomecomingModelGateway.ChatMessage> build(
                HomecomingSupervisionRepository.Event event,
                String ownerId,
                String timelineId,
                String trigger,
                long now);
    }

    interface GatewayPort {
        void stream(
                String requestId,
                String routeId,
                String modelId,
                List<HomecomingModelGateway.ChatMessage> messages,
                HomecomingModelGateway.StreamObserver observer);
    }

    interface MessagePort {
        String commit(
                HomecomingSupervisionRepository.Event event,
                String ownerId,
                String timelineId,
                String text,
                long now);
    }

    interface ControlPort {
        void apply(
                HomecomingSupervisionRepository.Event event,
                String ownerId,
                String timelineId,
                List<HomecomingControlParser.ControlEvent> controls,
                long now);
    }

    interface Clock {
        long now();
    }

    public interface Completion {
        void onComplete(String messageId, String text);
        void onFailure(String code);
        void onDuplicate();
    }

    private static final class RoleTarget {
        final String ownerId;
        final String timelineId;

        RoleTarget(String ownerId, String timelineId) {
            this.ownerId = ownerId;
            this.timelineId = timelineId;
        }
    }
}
