package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionStatusTest {
    @Test
    public void projectsConfiguredNamesUsageCheckpointsAndEffectiveState()
            throws Exception {
        JSONObject state = new JSONObject()
                .put("groups", new JSONArray().put(new JSONObject()
                        .put("groupId", "focus")
                        .put("displayName", "专注应用")
                        .put("roleId", "connor")
                        .put("roundUsageMs", 720_000L)
                        .put("checkpointsMinutes", new JSONArray()
                                .put(10).put(20))
                        .put("effectiveState", "TEMP_UNLOCK")
                        .put("lock", JSONObject.NULL)
                        .put("temporaryUnlock", new JSONObject()
                                .put("deadlineWallMs", 9_000L))));
        HomecomingSupervisionAdapter.Snapshot snapshot =
                new HomecomingSupervisionAdapter.Snapshot(
                        true, "ready", state);

        JSONObject status = HomecomingRuntime.supervisionStatus(
                snapshot, "主伴侣配置名", "第二伴侣配置名");

        assertTrue(status.getBoolean("enabled"));
        assertEquals("ready", status.getString("readiness"));
        JSONObject group = status.getJSONArray("groups").getJSONObject(0);
        assertEquals("专注应用", group.getString("displayName"));
        assertEquals("第二伴侣配置名", group.getString("roleLabel"));
        assertEquals(720_000L, group.getLong("roundUsageMs"));
        assertEquals(2, group.getJSONArray("checkpointsMinutes").length());
        assertEquals("TEMP_UNLOCK", group.getString("effectiveState"));
        assertFalse(group.has("roleId"));
    }

    @Test
    public void unavailableSnapshotReturnsSafeEmptyStatus() throws Exception {
        JSONObject status = HomecomingRuntime.supervisionStatus(
                new HomecomingSupervisionAdapter.Snapshot(
                        false, "unavailable", new JSONObject()),
                "主伴侣配置名",
                "第二伴侣配置名");

        assertFalse(status.getBoolean("enabled"));
        assertEquals("unavailable", status.getString("readiness"));
        assertEquals(0, status.getJSONArray("groups").length());
    }

    @Test
    public void unknownProtocolRoleDoesNotPretendToBeConfiguredCompanion()
            throws Exception {
        JSONObject state = new JSONObject()
                .put("groups", new JSONArray().put(new JSONObject()
                        .put("groupId", "focus")
                        .put("roleId", "unknown-role")));

        JSONObject status = HomecomingRuntime.supervisionStatus(
                new HomecomingSupervisionAdapter.Snapshot(
                        true, "ready", state),
                "主伴侣配置名",
                "第二伴侣配置名");

        assertEquals("", status.getJSONArray("groups")
                .getJSONObject(0).getString("roleLabel"));
    }
}
