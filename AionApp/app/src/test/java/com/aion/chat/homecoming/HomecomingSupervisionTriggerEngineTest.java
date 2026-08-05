package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionTriggerEngineTest {
    @Test
    public void checkpointUsesConfiguredRoleAndCompletesInSafeOrder() {
        ArrayList<String> order = new ArrayList<>();
        RecordingEvents events = new RecordingEvents(event("event-one", "connor"), order);
        RecordingCompletion completion = new RecordingCompletion();
        String[] selection = new String[2];
        HomecomingSupervisionTriggerEngine engine =
                new HomecomingSupervisionTriggerEngine(
                        events,
                        (value, owner, timeline, trigger, now) -> {
                            selection[0] = owner;
                            selection[1] = timeline;
                            assertTrue(trigger.contains("10"));
                            assertTrue(trigger.contains("focus"));
                            order.add("context");
                            return Collections.singletonList(
                                    new HomecomingModelGateway.ChatMessage(
                                            "system", value.payloadJson));
                        },
                        (requestId, routeId, modelId, messages, observer) -> {
                            order.add("gateway");
                            observer.onComplete("请先休息一下"
                                    + "[APP_TEMP_UNLOCK:focus|5|短暂解锁]");
                        },
                        (value, owner, timeline, text, now) -> {
                            order.add("message");
                            return "message-one";
                        },
                        (value, owner, timeline, controls, now) ->
                                order.add("controls"),
                        () -> 2_000L);

        engine.execute("event-one", "route", "model", completion);

        assertEquals("second", selection[0]);
        assertEquals("companion_private", selection[1]);
        assertEquals(
                java.util.Arrays.asList(
                        "claim", "context", "gateway", "message",
                        "controls", "complete"),
                order);
        assertTrue(completion.complete);
        assertEquals("请先休息一下", completion.text);
    }

    @Test
    public void duplicateNeverCallsCloudModel() {
        RecordingEvents events = new RecordingEvents(event("event-two", "aion"),
                new ArrayList<>());
        events.claimed = false;
        int[] calls = {0};
        RecordingCompletion completion = new RecordingCompletion();
        HomecomingSupervisionTriggerEngine engine =
                new HomecomingSupervisionTriggerEngine(
                        events,
                        (value, owner, timeline, trigger, now) ->
                                Collections.emptyList(),
                        (requestId, routeId, modelId, messages, observer) ->
                                calls[0]++,
                        (value, owner, timeline, text, now) -> "message",
                        (value, owner, timeline, controls, now) -> { },
                        () -> 2_000L);

        engine.execute("event-two", "route", "model", completion);

        assertEquals(0, calls[0]);
        assertTrue(completion.duplicate);
    }

    @Test
    public void failuresRemainRecoverable() {
        RecordingEvents events = new RecordingEvents(event("event-three", "aion"),
                new ArrayList<>());
        RecordingCompletion completion = new RecordingCompletion();
        HomecomingSupervisionTriggerEngine engine =
                new HomecomingSupervisionTriggerEngine(
                        events,
                        (value, owner, timeline, trigger, now) ->
                                Collections.emptyList(),
                        (requestId, routeId, modelId, messages, observer) ->
                                observer.onFailure("network_unavailable"),
                        (value, owner, timeline, text, now) -> "message",
                        (value, owner, timeline, controls, now) -> { },
                        () -> 2_000L);

        engine.execute("event-three", "route", "model", completion);

        assertEquals("network_unavailable", events.failure);
        assertEquals("network_unavailable", completion.failure);
    }

    private static HomecomingSupervisionRepository.Event event(
            String id, String roleId) {
        return new HomecomingSupervisionRepository.Event(
                id, "epoch", "focus", 600_000L, roleId,
                "{\"eventType\":\"checkpoint\"}", "pending",
                0, "", "", "", 1L, 1L);
    }

    private static final class RecordingEvents
            implements HomecomingSupervisionTriggerEngine.EventPort {
        final HomecomingSupervisionRepository.Event event;
        final List<String> order;
        boolean claimed = true;
        String failure = "";

        RecordingEvents(
                HomecomingSupervisionRepository.Event event,
                List<String> order) {
            this.event = event;
            this.order = order;
        }

        @Override public HomecomingSupervisionRepository.Event find(String eventId) {
            return event;
        }

        @Override public HomecomingSupervisionRepository.Claim claim(
                String eventId, long now) {
            order.add("claim");
            return new HomecomingSupervisionRepository.Claim(
                    eventId, claimed, claimed ? "running" : "complete", 1);
        }

        @Override public void complete(
                String eventId, String messageId, String resultText, long now) {
            order.add("complete");
        }

        @Override public void fail(String eventId, String diagnostic, long now) {
            failure = diagnostic;
        }
    }

    private static final class RecordingCompletion
            implements HomecomingSupervisionTriggerEngine.Completion {
        boolean complete;
        boolean duplicate;
        String text = "";
        String failure = "";

        @Override public void onComplete(String messageId, String value) {
            complete = true;
            text = value;
        }

        @Override public void onFailure(String code) {
            failure = code;
        }

        @Override public void onDuplicate() {
            duplicate = true;
        }
    }
}
