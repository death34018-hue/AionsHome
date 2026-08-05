package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionAdapterTest {
    @Test
    public void snapshotReadsOnlyLocalRuntimeAndPermissionPorts() throws Exception {
        HomecomingSupervisionAdapter adapter = new HomecomingSupervisionAdapter(
                new HomecomingSupervisionAdapter.RuntimePort() {
                    @Override public boolean enabled() {
                        return true;
                    }

                    @Override public JSONObject state() throws Exception {
                        return new JSONObject()
                                .put("groups", new JSONArray().put(new JSONObject()
                                        .put("groupId", "group-one")
                                        .put("displayName", "阅读")
                                        .put("roleId", "main")));
                    }
                },
                () -> "ready");

        HomecomingSupervisionAdapter.Snapshot snapshot = adapter.snapshot();

        assertTrue(snapshot.enabled);
        assertEquals("ready", snapshot.readiness);
        assertEquals("group-one", snapshot.state.getJSONArray("groups")
                .getJSONObject(0).getString("groupId"));
    }

    @Test
    public void runtimeFailureReturnsUnavailableSnapshot() {
        HomecomingSupervisionAdapter adapter = new HomecomingSupervisionAdapter(
                new HomecomingSupervisionAdapter.RuntimePort() {
                    @Override public boolean enabled() {
                        throw new IllegalStateException("runtime failed");
                    }

                    @Override public JSONObject state() {
                        throw new IllegalStateException("runtime failed");
                    }
                },
                () -> "ready");

        HomecomingSupervisionAdapter.Snapshot snapshot = adapter.snapshot();

        assertFalse(snapshot.enabled);
        assertEquals("unavailable", snapshot.readiness);
        assertEquals(0, snapshot.state.length());
    }

    @Test
    public void commandDelegatesToThePhoneLocalRuntimeAndKeepsConfiguredGroupName() {
        HomecomingSupervisionAdapter adapter = new HomecomingSupervisionAdapter(
                new HomecomingSupervisionAdapter.RuntimePort() {
                    @Override public boolean enabled() {
                        return true;
                    }

                    @Override public JSONObject state() {
                        return new JSONObject();
                    }
                },
                () -> "ready",
                (action, groupId, minutes, roleId, message, commandId, expiresAt) ->
                        new HomecomingSupervisionCommandHandler.RuntimeResult(
                                true, "", "配置应用名"));

        HomecomingSupervisionCommandHandler.RuntimeResult result =
                adapter.applyCommand(
                        "lock", "group-one", 20, "aion", "休息",
                        "command-one", 300_000L);

        assertTrue(result.success);
        assertEquals("配置应用名", result.groupDisplayName);
    }
}
