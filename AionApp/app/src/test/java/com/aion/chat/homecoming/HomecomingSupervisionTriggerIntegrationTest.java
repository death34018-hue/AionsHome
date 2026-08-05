package com.aion.chat.homecoming;

import org.json.JSONObject;
import org.junit.Test;

import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionTriggerIntegrationTest {
    @Test
    public void unavailableSupervisionContextDoesNotFailScheduledReminder() {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(() ->
                "{\"user\":{\"name\":\"U\",\"persona\":\"UP\"},\"companions\":{"
                        + "\"main\":{\"name\":\"M\",\"persona\":\"MP\"},"
                        + "\"second\":{\"name\":\"S\",\"persona\":\"SP\"}}}");
        HomecomingContextBuilder context = new HomecomingContextBuilder(
                identities,
                (owner, query, limit) -> Collections.emptyList(),
                (timeline, before, limit) -> Collections.emptyList(),
                4000,
                new HomecomingSupervisionContext(
                        () -> new HomecomingSupervisionAdapter.Snapshot(
                                true, "unavailable", new JSONObject())));
        HomecomingScheduleRepository.Schedule schedule =
                new HomecomingScheduleRepository.Schedule(
                        "schedule-one", "reminder", 900L, "喝水",
                        "main", "main_private", "", "active",
                        1L, 1L, null);
        RecordingCompletion completion = new RecordingCompletion();
        boolean[] completed = {false};
        boolean[] contextHadSupervision = {false};
        HomecomingTriggerEngine engine = new HomecomingTriggerEngine(
                new HomecomingTriggerEngine.SchedulePort() {
                    @Override public HomecomingScheduleRepository.Schedule find(String id) {
                        return schedule;
                    }

                    @Override public HomecomingScheduleRepository.ExecutionClaim claim(
                            String id, long triggerAt, long now) {
                        return new HomecomingScheduleRepository.ExecutionClaim(
                                "execution-one", id, triggerAt, true, "claimed");
                    }

                    @Override public void complete(
                            String executionId, String messageId, long now) {
                        completed[0] = true;
                    }

                    @Override public void fail(
                            String executionId, String diagnostic, long now) {
                    }
                },
                (value, trigger, now) -> {
                    List<HomecomingModelGateway.ChatMessage> messages =
                            context.build(
                                    value.timelineId, value.ownerId, trigger, now, "");
                    contextHadSupervision[0] =
                            messages.get(0).text.contains("APP_LOCK");
                    return messages;
                },
                (requestId, routeId, modelId, messages, observer) ->
                        observer.onComplete("记得喝水"),
                (value, executionId, text, now) -> "message-one",
                (executionId, value, controls, now) -> {
                },
                () -> 1_000L);

        engine.execute(
                "schedule-one", 900L, "route", "model", completion);

        assertTrue(completed[0]);
        assertTrue(completion.complete);
        assertFalse(contextHadSupervision[0]);
    }

    private static final class RecordingCompletion
            implements HomecomingTriggerEngine.Completion {
        boolean complete;
        @Override public void onComplete(String messageId, String text) {
            complete = true;
        }
        @Override public void onFailure(String code) {
        }
        @Override public void onDuplicate() {
        }
    }
}
