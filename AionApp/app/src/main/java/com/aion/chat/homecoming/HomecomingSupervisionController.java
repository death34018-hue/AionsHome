package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.List;

public final class HomecomingSupervisionController {
    private final ModePort mode;
    private final RuntimePort runtime;
    private final EventPort events;
    private final DispatchPort dispatch;
    private final Clock clock;
    private boolean attached;

    HomecomingSupervisionController(
            ModePort mode,
            RuntimePort runtime,
            EventPort events,
            DispatchPort dispatch,
            Clock clock) {
        this.mode = required(mode, "mode");
        this.runtime = required(runtime, "runtime");
        this.events = required(events, "events");
        this.dispatch = required(dispatch, "dispatch");
        this.clock = required(clock, "clock");
    }

    public void start() {
        if (!mode.isActive() || attached) return;
        runtime.setListener(this::onStateEvent);
        attached = true;
        for (HomecomingSupervisionRepository.Event event :
                events.recoverable(clock.now())) {
            dispatch.fire(event.eventId);
        }
    }

    public void stop() {
        if (!attached) return;
        runtime.setListener(null);
        attached = false;
    }

    private void onStateEvent(
            String eventType, String groupId, long checkpointMs) {
        if (!attached || !mode.isActive()
                || !"checkpoint".equals(eventType)
                || checkpointMs <= 0L) {
            return;
        }
        try {
            JSONObject payload = runtime.buildStatePayload(
                    eventType, groupId, checkpointMs);
            String eventId = payload.getString("eventId");
            String roleId = roleFor(payload.optJSONArray("groups"), groupId);
            if (roleId.isEmpty()) return;
            events.enqueue(
                    eventId, groupId, checkpointMs, roleId,
                    payload.toString(), clock.now());
            dispatch.fire(eventId);
        } catch (Exception ignored) {
            // A malformed local state event must not disturb the foreground runtime.
        }
    }

    private static String roleFor(JSONArray groups, String groupId) {
        if (groups == null) return "";
        for (int index = 0; index < groups.length(); index++) {
            JSONObject group = groups.optJSONObject(index);
            if (group != null && groupId.equals(group.optString("groupId", ""))) {
                return group.optString("roleId", "").trim();
            }
        }
        return "";
    }

    private static <T> T required(T value, String label) {
        if (value == null) throw new IllegalArgumentException(label + " is required");
        return value;
    }

    interface ModePort {
        boolean isActive();
    }

    interface RuntimePort {
        void setListener(StateListener listener);
        JSONObject buildStatePayload(
                String eventType, String groupId, long checkpointMs) throws Exception;
    }

    interface EventPort {
        HomecomingSupervisionRepository.Event enqueue(
                String eventId,
                String groupId,
                long checkpointMs,
                String roleId,
                String payloadJson,
                long now);
        List<HomecomingSupervisionRepository.Event> recoverable(long now);
    }

    interface DispatchPort {
        void fire(String eventId);
    }

    interface StateListener {
        void onStateEvent(String eventType, String groupId, long checkpointMs);
    }

    interface Clock {
        long now();
    }
}
