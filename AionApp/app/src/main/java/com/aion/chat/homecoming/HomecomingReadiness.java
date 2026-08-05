package com.aion.chat.homecoming;

import org.json.JSONObject;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

public final class HomecomingReadiness {
    public static final int FRONTEND_VERSION = 1;

    public final boolean ready;
    public final long snapshotCreatedAt;
    public final long lastCheckedAt;
    public final int mainMemoryCount;
    public final int secondMemoryCount;
    public final int mainMessageCount;
    public final int secondMessageCount;
    public final int groupMessageCount;
    public final int pendingScheduleCount;
    public final int portableRouteCount;
    public final int schemaVersion;
    public final int frontendVersion;
    public final Map<String, String> permissionStates;
    public final String scheduleExactness;
    public final String warning;

    public HomecomingReadiness(
            boolean ready,
            long snapshotCreatedAt,
            long lastCheckedAt,
            int mainMemoryCount,
            int secondMemoryCount,
            int mainMessageCount,
            int secondMessageCount,
            int groupMessageCount,
            int pendingScheduleCount,
            int portableRouteCount,
            int schemaVersion,
            Map<String, String> permissionStates,
            String warning) {
        this.ready = ready;
        this.snapshotCreatedAt = snapshotCreatedAt;
        this.lastCheckedAt = lastCheckedAt;
        this.mainMemoryCount = mainMemoryCount;
        this.secondMemoryCount = secondMemoryCount;
        this.mainMessageCount = mainMessageCount;
        this.secondMessageCount = secondMessageCount;
        this.groupMessageCount = groupMessageCount;
        this.pendingScheduleCount = pendingScheduleCount;
        this.portableRouteCount = portableRouteCount;
        this.schemaVersion = schemaVersion;
        this.frontendVersion = FRONTEND_VERSION;
        this.permissionStates = Collections.unmodifiableMap(
                new LinkedHashMap<>(permissionStates));
        this.scheduleExactness = this.permissionStates.containsKey("schedule_exactness")
                ? this.permissionStates.get("schedule_exactness") : "unknown";
        this.warning = warning == null ? "" : warning;
    }

    public int totalMessageCount() {
        return mainMessageCount + secondMessageCount + groupMessageCount;
    }

    static HomecomingReadiness fromPayload(
            JSONObject payload, int portableRouteCount, long checkedAt) throws Exception {
        JSONObject sections = payload.getJSONObject("sections");
        JSONObject memories = sections.getJSONObject("memories");
        JSONObject timelines = sections.getJSONObject("timelines");
        return new HomecomingReadiness(
                true,
                payload.optLong("created_at", 0L),
                checkedAt,
                memories.optJSONArray("main") == null
                        ? 0 : memories.getJSONArray("main").length(),
                memories.optJSONArray("second") == null
                        ? 0 : memories.getJSONArray("second").length(),
                messageCount(timelines, "main_private"),
                messageCount(timelines, "companion_private"),
                messageCount(timelines, "group"),
                sections.optJSONArray("schedules") == null
                        ? 0 : sections.getJSONArray("schedules").length(),
                portableRouteCount,
                payload.getInt("schema"),
                Collections.<String, String>emptyMap(),
                "");
    }

    public JSONObject toJson() {
        try {
            return new JSONObject()
                    .put("ready", ready)
                    .put("snapshotCreatedAt", snapshotCreatedAt)
                    .put("lastCheckedAt", lastCheckedAt)
                    .put("mainMemoryCount", mainMemoryCount)
                    .put("secondMemoryCount", secondMemoryCount)
                    .put("mainMessageCount", mainMessageCount)
                    .put("secondMessageCount", secondMessageCount)
                    .put("groupMessageCount", groupMessageCount)
                    .put("pendingScheduleCount", pendingScheduleCount)
                    .put("portableRouteCount", portableRouteCount)
                    .put("schemaVersion", schemaVersion)
                    .put("frontendVersion", frontendVersion)
                    .put("permissionStates", new JSONObject(permissionStates))
                    .put("scheduleExactness", scheduleExactness)
                    .put("warning", warning);
        } catch (org.json.JSONException exception) {
            throw new IllegalStateException("could not encode readiness", exception);
        }
    }

    private static int messageCount(JSONObject timelines, String name) throws Exception {
        JSONObject timeline = timelines.optJSONObject(name);
        return timeline == null || timeline.optJSONArray("messages") == null
                ? 0 : timeline.getJSONArray("messages").length();
    }
}
