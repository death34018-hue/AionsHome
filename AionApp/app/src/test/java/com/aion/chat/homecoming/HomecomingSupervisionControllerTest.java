package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

public class HomecomingSupervisionControllerTest {
    @Test
    public void inactiveModeNeverAttachesOrDispatches() {
        RecordingRuntime runtime = new RecordingRuntime();
        RecordingEvents events = new RecordingEvents();
        ArrayList<String> dispatched = new ArrayList<>();
        HomecomingSupervisionController controller =
                new HomecomingSupervisionController(
                        () -> false, runtime, events, dispatched::add, () -> 100L);

        controller.start();

        assertNull(runtime.listener);
        assertEquals(Collections.emptyList(), dispatched);
    }

    @Test
    public void startAttachesAndRecoversPersistedWork() {
        RecordingRuntime runtime = new RecordingRuntime();
        RecordingEvents events = new RecordingEvents();
        events.recoverable.add(event("old-event", "aion"));
        ArrayList<String> dispatched = new ArrayList<>();
        HomecomingSupervisionController controller =
                new HomecomingSupervisionController(
                        () -> true, runtime, events, dispatched::add, () -> 100L);

        controller.start();

        assertEquals(1, runtime.attachCount);
        assertEquals(Collections.singletonList("old-event"), dispatched);
    }

    @Test
    public void checkpointIsPersistedBeforeDispatch() throws Exception {
        ArrayList<String> order = new ArrayList<>();
        RecordingRuntime runtime = new RecordingRuntime();
        runtime.payload = new JSONObject()
                .put("eventId", "checkpoint-one")
                .put("groups", new JSONArray().put(new JSONObject()
                        .put("groupId", "focus")
                        .put("roleId", "connor")));
        RecordingEvents events = new RecordingEvents();
        events.order = order;
        HomecomingSupervisionController controller =
                new HomecomingSupervisionController(
                        () -> true,
                        runtime,
                        events,
                        id -> order.add("dispatch:" + id),
                        () -> 200L);
        controller.start();

        runtime.listener.onStateEvent("checkpoint", "focus", 600_000L);

        assertEquals("connor", events.enqueued.roleId);
        assertEquals(600_000L, events.enqueued.checkpointMs);
        assertEquals(
                java.util.Arrays.asList(
                        "enqueue:checkpoint-one", "dispatch:checkpoint-one"),
                order);
    }

    @Test
    public void nonCheckpointDoesNotWakeModelAndStopDetaches() {
        RecordingRuntime runtime = new RecordingRuntime();
        RecordingEvents events = new RecordingEvents();
        ArrayList<String> dispatched = new ArrayList<>();
        HomecomingSupervisionController controller =
                new HomecomingSupervisionController(
                        () -> true, runtime, events, dispatched::add, () -> 100L);
        controller.start();

        runtime.listener.onStateEvent("enter", "focus", 0L);
        runtime.listener.onStateEvent("exit", "focus", 0L);
        controller.stop();

        assertNull(events.enqueued);
        assertEquals(Collections.emptyList(), dispatched);
        assertNull(runtime.listener);
        assertEquals(1, runtime.detachCount);
    }

    private static HomecomingSupervisionRepository.Event event(
            String id, String roleId) {
        return new HomecomingSupervisionRepository.Event(
                id, "epoch", "focus", 600_000L, roleId, "{}",
                "pending", 0, "", "", "", 1L, 1L);
    }

    private static final class RecordingRuntime
            implements HomecomingSupervisionController.RuntimePort {
        HomecomingSupervisionController.StateListener listener;
        JSONObject payload = new JSONObject();
        int attachCount;
        int detachCount;

        @Override public void setListener(
                HomecomingSupervisionController.StateListener value) {
            listener = value;
            if (value == null) detachCount++;
            else attachCount++;
        }

        @Override public JSONObject buildStatePayload(
                String eventType, String groupId, long checkpointMs) {
            return payload;
        }
    }

    private static final class RecordingEvents
            implements HomecomingSupervisionController.EventPort {
        final List<HomecomingSupervisionRepository.Event> recoverable =
                new ArrayList<>();
        HomecomingSupervisionRepository.Event enqueued;
        List<String> order;

        @Override public HomecomingSupervisionRepository.Event enqueue(
                String eventId, String groupId, long checkpointMs,
                String roleId, String payloadJson, long now) {
            if (order != null) order.add("enqueue:" + eventId);
            enqueued = event(eventId, roleId);
            return enqueued;
        }

        @Override public List<HomecomingSupervisionRepository.Event> recoverable(
                long now) {
            return recoverable;
        }
    }
}
