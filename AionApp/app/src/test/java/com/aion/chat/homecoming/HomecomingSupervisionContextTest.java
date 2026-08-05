package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionContextTest {
    @Test
    public void enabledSnapshotUsesConfiguredLabelsAndExistingCommands() throws Exception {
        JSONObject state = new JSONObject()
                .put("groups", new JSONArray().put(new JSONObject()
                        .put("groupId", "group-video")
                        .put("displayName", "短视频")
                        .put("roleId", "second")
                        .put("roundUsageMs", 1_200_000L)
                        .put("effectiveState", "NORMAL")));
        HomecomingSupervisionContext context = new HomecomingSupervisionContext(
                () -> new HomecomingSupervisionAdapter.Snapshot(
                        true, "ready", state));

        String text = context.build("自定义主伴侣", "自定义第二伴侣");

        assertTrue(text.contains("短视频"));
        assertTrue(text.contains("group-video"));
        assertTrue(text.contains("自定义第二伴侣"));
        assertTrue(text.contains("[APP_LOCK:groupId|分钟|提示]"));
        assertTrue(text.contains("[APP_TEMP_UNLOCK:groupId|分钟|说明]"));
        assertTrue(text.contains("[APP_UNLOCK:groupId]"));
        assertFalse(text.contains("Connor"));
        assertFalse(text.contains("Aion"));
        assertFalse(text.contains("Ithil"));
    }

    @Test
    public void degradedReadinessIsTruthfulWithoutDisablingExistingLocks()
            throws Exception {
        JSONObject state = new JSONObject().put("groups", new JSONArray());
        HomecomingSupervisionContext context = new HomecomingSupervisionContext(
                () -> new HomecomingSupervisionAdapter.Snapshot(
                        true, "degraded", state));

        String text = context.build("主伴侣", "第二伴侣");

        assertTrue(text.contains("降级"));
        assertTrue(text.contains("已有锁定仍由手机本地执行"));
    }

    @Test
    public void disabledOrUnavailableSupervisionAddsNoModelCapability()
            throws Exception {
        HomecomingSupervisionContext disabled = new HomecomingSupervisionContext(
                () -> new HomecomingSupervisionAdapter.Snapshot(
                        false, "ready", new JSONObject()));
        HomecomingSupervisionContext unavailable = new HomecomingSupervisionContext(
                () -> new HomecomingSupervisionAdapter.Snapshot(
                        true, "unavailable", new JSONObject()));

        assertTrue(disabled.build("主伴侣", "第二伴侣").isEmpty());
        assertTrue(unavailable.build("主伴侣", "第二伴侣").isEmpty());
    }

    @Test
    public void malformedStateFailsClosedWithoutBreakingChatContext() {
        HomecomingSupervisionContext context = new HomecomingSupervisionContext(
                () -> {
                    throw new IllegalStateException("bad local state");
                });

        assertTrue(context.build("主伴侣", "第二伴侣").isEmpty());
    }
}
