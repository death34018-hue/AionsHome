package com.aion.chat.homecoming;

import java.text.SimpleDateFormat;
import java.util.List;
import java.util.Locale;

public final class HomecomingTriggerEngine {
    private final SchedulePort schedules;
    private final ContextPort context;
    private final GatewayPort gateway;
    private final MessagePort messages;
    private final ControlPort controls;
    private final Clock clock;

    HomecomingTriggerEngine(
            SchedulePort schedules,
            ContextPort context,
            GatewayPort gateway,
            MessagePort messages,
            ControlPort controls,
            Clock clock) {
        this.schedules = schedules;
        this.context = context;
        this.gateway = gateway;
        this.messages = messages;
        this.controls = controls;
        this.clock = clock;
    }

    public void execute(
            String scheduleId,
            long triggerAt,
            String routeId,
            String modelId,
            Completion completion) {
        HomecomingScheduleRepository.Schedule schedule = schedules.find(scheduleId);
        if (schedule == null || !"active".equals(schedule.status)) {
            completion.onFailure("unknown_schedule");
            return;
        }
        if (schedule.triggerAt != triggerAt) {
            completion.onFailure("trigger_mismatch");
            return;
        }
        long now = clock.now();
        HomecomingScheduleRepository.ExecutionClaim claim =
                schedules.claim(schedule.id, triggerAt, now);
        if (!claim.claimed) {
            completion.onDuplicate();
            return;
        }
        String trigger = trigger(schedule, now);
        final List<HomecomingModelGateway.ChatMessage> contextMessages;
        try {
            contextMessages = context.build(schedule, trigger, now);
        } catch (RuntimeException exception) {
            fail(claim.executionId, "context_failed", completion);
            return;
        }
        try {
            gateway.stream(
                    "schedule:" + claim.executionId,
                    routeId,
                    modelId,
                    contextMessages,
                    new HomecomingModelGateway.StreamObserver() {
                        @Override public void onChunk(String text) {
                            // Background notifications publish only complete replies.
                        }

                        @Override public void onComplete(String text) {
                            HomecomingControlParser.Result parsed =
                                    HomecomingControlParser.parse(text);
                            if (parsed.visibleText.isEmpty()) {
                                fail(claim.executionId, "empty_model_reply", completion);
                                return;
                            }
                            try {
                                long completedAt = clock.now();
                                String messageId = messages.commit(
                                        schedule,
                                        claim.executionId,
                                        parsed.visibleText,
                                        completedAt);
                                controls.apply(
                                        claim.executionId,
                                        schedule,
                                        parsed.events,
                                        completedAt);
                                schedules.complete(
                                        claim.executionId, messageId, completedAt);
                                completion.onComplete(messageId, parsed.visibleText);
                            } catch (RuntimeException exception) {
                                fail(claim.executionId, "trigger_commit_failed", completion);
                            }
                        }

                        @Override public void onFailure(String code) {
                            fail(claim.executionId,
                                    code == null || code.trim().isEmpty()
                                            ? "model_request_failed" : code,
                                    completion);
                        }
                    });
        } catch (RuntimeException exception) {
            fail(claim.executionId, "model_request_failed", completion);
        }
    }

    private void fail(String executionId, String code, Completion completion) {
        schedules.fail(executionId, code, clock.now());
        completion.onFailure(code);
    }

    private static String trigger(
            HomecomingScheduleRepository.Schedule schedule, long now) {
        String time = new SimpleDateFormat(
                "yyyy-MM-dd HH:mm:ss", Locale.ROOT).format(new java.util.Date(now));
        StringBuilder prompt = new StringBuilder();
        if ("monitor".equals(schedule.type)) {
            prompt.append("[定时监控触发]\n");
        } else {
            prompt.append("[日程闹铃触发]\n");
        }
        prompt.append("日程内容：")
                .append(schedule.content)
                .append("\n当前时间：")
                .append(time)
                .append("\n当前时间已经到达，请按既有人设自然提醒用户。");
        if ("monitor".equals(schedule.type)) {
            prompt.append("\n本次没有可用的实时摄像头画面，"
                    + "只能依据文字状态回复，不得虚构用户或现场画面。");
        }
        return prompt.toString();
    }

    interface SchedulePort {
        HomecomingScheduleRepository.Schedule find(String id);
        HomecomingScheduleRepository.ExecutionClaim claim(
                String scheduleId, long triggerAt, long now);
        void complete(String executionId, String messageId, long now);
        void fail(String executionId, String diagnostic, long now);
    }

    interface ContextPort {
        List<HomecomingModelGateway.ChatMessage> build(
                HomecomingScheduleRepository.Schedule schedule,
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
                HomecomingScheduleRepository.Schedule schedule,
                String executionId,
                String completeText,
                long now);
    }

    interface ControlPort {
        void apply(
                String executionId,
                HomecomingScheduleRepository.Schedule schedule,
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
}
