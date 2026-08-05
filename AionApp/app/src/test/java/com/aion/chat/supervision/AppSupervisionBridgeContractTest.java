package com.aion.chat.supervision;

import org.json.JSONObject;
import org.junit.Test;

import java.lang.reflect.Method;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

public class AppSupervisionBridgeContractTest {
    @Test
    public void exposesRequiredJavascriptContract() throws Exception {
        assertMethod("getSnapshot");
        assertMethod("listLaunchableApps");
        assertMethod("setRoleCatalog", String.class);
        assertMethod("upsertGroup", String.class);
        assertMethod("removeGroup", String.class);
        assertMethod("setFeatureEnabled", boolean.class);
        assertMethod("clearRound", String.class);
        assertMethod("debugSetLock", String.class);
        assertMethod("debugSetTemporaryUnlock", String.class);
        assertMethod("debugRemoveLock", String.class);
        assertMethod("emergencyAction", String.class);
        assertMethod("canDrawOverlays");
        assertMethod("openOverlaySettings");
    }

    @Test
    public void malformedJsonReturnsErrorInsteadOfThrowingAcrossBridge() throws Exception {
        AppSupervisionBridge bridge = harness();

        JSONObject response = new JSONObject(bridge.upsertGroup("{broken"));

        assertFalse(response.getBoolean("ok"));
        assertFalse(response.getString("error").isEmpty());
    }

    @Test
    public void configuredRoleCatalogIsForwardedToRuntimeOverlay() throws Exception {
        RecordingOverlay overlay = new RecordingOverlay();
        AppSupervisionBridge bridge = harness(overlay);
        String catalog = new JSONObject()
                .put("roles", new org.json.JSONArray().put(new JSONObject()
                        .put("id", "role-main")
                        .put("label", "Configured Role")))
                .toString();

        JSONObject response = new JSONObject(bridge.setRoleCatalog(catalog));

        assertTrue(response.getBoolean("ok"));
        assertEquals("Configured Role", overlay.roleLabel("role-main"));
        assertEquals("AI", overlay.roleLabel("unknown-role"));
    }

    @Test
    public void systemWhitelistPackagesCannotBeManaged() throws Exception {
        AppSupervisionBridge bridge = harness();
        String request = new JSONObject()
                .put("groupId", "system-group")
                .put("displayName", "系统界面")
                .put("packageNames", new org.json.JSONArray()
                        .put("com.android.systemui"))
                .put("monitored", true)
                .put("idleMinutes", 30)
                .put("checkpointsMinutes", new org.json.JSONArray().put(20))
                .put("roleId", "role-main")
                .toString();

        JSONObject response = new JSONObject(bridge.upsertGroup(request));

        assertFalse(response.getBoolean("ok"));
        assertTrue(response.getString("error").contains("protected"));
    }

    @Test
    public void debugDirectiveMutationsDelegateToRuntime() throws Exception {
        AppSupervisionBridge bridge = harness();
        String request = new JSONObject()
                .put("groupId", "group-1")
                .put("minutes", 20)
                .put("roleId", "role-main")
                .put("message", "稍后再来")
                .put("commandId", "debug-1")
                .toString();

        assertTrue(new JSONObject(bridge.debugSetLock(request)).getBoolean("ok"));
        JSONObject snapshot = new JSONObject(bridge.getSnapshot());
        assertTrue(snapshot.getBoolean("ok"));
        assertEquals("LOCKED", snapshot.getJSONArray("groups")
                .getJSONObject(0).getString("effectiveState"));
    }

    @Test
    public void snapshotExposesWholeDeviceLockState() throws Exception {
        AppSupervisionRuntime runtime = runtime(new AppSupervisionOverlay());
        long future = runtime.elapsedRealtime() + 1_800_000_000_000L;
        assertTrue(runtime.applyAiCommand(
                "device_lock", "", 20, "aion", "整机休息",
                "device-bridge", future).isSuccess());

        JSONObject snapshot = new JSONObject(
                new AppSupervisionBridge(null, runtime).getSnapshot());

        assertEquals("LOCKED",
                snapshot.getJSONObject("deviceLock").getString("effectiveState"));
        assertEquals("device-bridge", snapshot.getJSONObject("deviceLock")
                .getJSONObject("lock").getString("commandId"));
    }

    private static void assertMethod(String name, Class<?>... parameterTypes)
            throws Exception {
        Method method = AppSupervisionBridge.class.getMethod(name, parameterTypes);
        assertNotNull(method.getAnnotation(android.webkit.JavascriptInterface.class));
    }

    private static AppSupervisionBridge harness() {
        return harness(new AppSupervisionOverlay());
    }

    private static AppSupervisionBridge harness(AppSupervisionOverlay overlay) {
        return new AppSupervisionBridge(null, runtime(overlay));
    }

    private static AppSupervisionRuntime runtime(AppSupervisionOverlay overlay) {
        AppGroup group = AppGroup.create(
                "group-1", "示例应用", Collections.singletonList("com.example.main"), true,
                SupervisionPolicy.of(
                        30 * 60_000L,
                        Collections.singletonList(20 * 60_000L),
                        "role-main"));
        AppSupervisionEngine engine = new AppSupervisionEngine(
                true, Collections.singletonList(group));
        AppSupervisionStore store = new AppSupervisionStore(new MemoryBackend());
        AppSupervisionRuntime runtime = new AppSupervisionRuntime(
                null,
                engine,
                store,
                new ForegroundAppDetector(),
                new AccessibilityRecoveryController(),
                overlay,
                new TestScheduler());
        return runtime;
    }

    private static final class RecordingOverlay extends AppSupervisionOverlay {}

    private static final class TestScheduler implements AppSupervisionRuntime.Scheduler {
        @Override public long elapsedRealtime() { return 10_000L; }
        @Override public long currentTimeMillis() { return 1_800_000_000_000L; }
        @Override public void schedule(Runnable runnable, long delayMs) {}
        @Override public void cancelAll() {}
    }

    private static final class MemoryBackend implements AppSupervisionStore.Backend {
        private final Map<String, String> values = new LinkedHashMap<>();
        @Override public String getString(String key, String defaultValue) {
            String value = values.get(key);
            return value == null ? defaultValue : value;
        }
        @Override public void putString(String key, String value) { values.put(key, value); }
    }
}
