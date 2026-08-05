package com.aion.chat.supervision;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

public class AppSupervisionStoreTest {
    private static final long HOUR_MS = 60L * 60L * 1000L;

    @Test
    public void configRoundTripPreservesUnicodeAndPackageGrouping() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        AppGroup group = AppGroup.create(
                "group-xhs",
                "小红书与分身",
                Arrays.asList("com.xingin.xhs", "com.xingin.xhs.clone"),
                true,
                SupervisionPolicy.of(
                        30 * 60_000L,
                        Arrays.asList(20 * 60_000L, 40 * 60_000L),
                        "role-second"));

        store.saveConfig(new AppSupervisionStore.ConfigSnapshot(
                true, Collections.singletonList(group)));
        AppSupervisionStore.ConfigSnapshot loaded = store.loadConfig();

        assertTrue(loaded.isFeatureEnabled());
        assertEquals(1, loaded.getGroups().size());
        AppGroup restored = loaded.getGroups().get(0);
        assertEquals("小红书与分身", restored.getDisplayName());
        assertEquals(Arrays.asList("com.xingin.xhs", "com.xingin.xhs.clone"),
                restored.getPackageNames());
        assertEquals("role-second", restored.getPolicy().getRoleId());
    }

    @Test
    public void runtimeRoundTripPreservesUnicodeDirectiveMessage() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        TimedDirective lock = TimedDirective.create(
                10_000L, 1_800_000_000_000L, 20,
                "role-main", "先去喝水，再回来。", "cmd-unicode");
        AppSupervisionStore.PersistedGroupState groupState =
                new AppSupervisionStore.PersistedGroupState(
                        12_345L,
                        9_000L,
                        new LinkedHashSet<>(Arrays.asList(5_000L, 10_000L)),
                        lock,
                        null);
        Map<String, AppSupervisionStore.PersistedGroupState> states = new LinkedHashMap<>();
        states.put("group-xhs", groupState);

        store.saveBootScopedState("boot-a", new AppSupervisionStore.RuntimeSnapshot(states));
        AppSupervisionStore.RuntimeSnapshot loaded = store.loadBootScopedState("boot-a");

        AppSupervisionStore.PersistedGroupState restored = loaded.getStates().get("group-xhs");
        assertEquals(12_345L, restored.getRoundUsageMs());
        assertEquals("先去喝水，再回来。", restored.getLock().getMessage());
        assertEquals("cmd-unicode", restored.getLock().getCommandId());
        assertEquals(2, restored.getFiredCheckpointsMs().size());
    }

    @Test
    public void runtimeRoundTripPreservesDeviceLockIndependentlyFromGroups() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        TimedDirective deviceLock = TimedDirective.create(
                20_000L, 1_800_000_000_000L, 45,
                "role-second", "整机休息", "cmd-device-lock");
        TimedDirective deviceTemporaryUnlock = TimedDirective.create(
                30_000L, 1_800_000_010_000L, 10,
                "role-main", "临时处理消息", "cmd-device-temporary");
        AppSupervisionStore.PersistedDeviceState deviceState =
                new AppSupervisionStore.PersistedDeviceState(
                        deviceLock, deviceTemporaryUnlock);

        store.saveBootScopedState(
                "boot-a",
                new AppSupervisionStore.RuntimeSnapshot(
                        Collections.<String, AppSupervisionStore.PersistedGroupState>
                                emptyMap(),
                        deviceState));
        AppSupervisionStore.RuntimeSnapshot loaded =
                store.loadBootScopedState("boot-a");

        assertEquals("cmd-device-lock",
                loaded.getDeviceState().getLock().getCommandId());
        assertEquals("cmd-device-temporary",
                loaded.getDeviceState().getTemporaryUnlock().getCommandId());
        assertTrue(loaded.getStates().isEmpty());
    }

    @Test
    public void changedBootIdDropsRuntimeButRetainsConfiguration() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        AppGroup group = AppGroup.create(
                "group-1", "示例应用", Collections.singletonList("com.example"), true,
                SupervisionPolicy.of(30_000L, Collections.singletonList(10_000L), "role-main"));
        store.saveConfig(new AppSupervisionStore.ConfigSnapshot(
                true, Collections.singletonList(group)));
        Map<String, AppSupervisionStore.PersistedGroupState> states = new LinkedHashMap<>();
        states.put("group-1", new AppSupervisionStore.PersistedGroupState(
                5_000L, null, Collections.<Long>emptySet(), null, null));
        store.saveBootScopedState("boot-a", new AppSupervisionStore.RuntimeSnapshot(states));

        AppSupervisionStore.RuntimeSnapshot afterReboot = store.loadBootScopedState("boot-b");

        assertTrue(afterReboot.getStates().isEmpty());
        assertNull(afterReboot.getDeviceState().getLock());
        assertNull(afterReboot.getDeviceState().getTemporaryUnlock());
        assertEquals(1, store.loadConfig().getGroups().size());
        assertEquals("", backend.getString("runtime_json", ""));
    }

    @Test
    public void logsPruneAfterFortyEightHoursAndCapAtFiveHundred() {
        MemoryBackend backend = new MemoryBackend();
        AppSupervisionStore store = new AppSupervisionStore(backend);
        long nowWall = 1_800_000_000_000L;
        store.appendLog(logAt(nowWall - 49L * HOUR_MS));
        store.appendLog(logAt(nowWall - 47L * HOUR_MS));

        assertEquals(1, store.readLogs(nowWall).size());

        for (int i = 0; i < 510; i++) {
            store.appendLog(new AppSupervisionStore.LogRecord(
                    nowWall + i, "diagnostic", "group-" + i, "message-" + i));
        }
        List<AppSupervisionStore.LogRecord> capped = store.readLogs(nowWall + 510);
        assertEquals(500, capped.size());
        assertEquals("group-10", capped.get(0).getGroupId());
    }

    @Test
    public void malformedJsonReturnsDefaultsAndRecordsRecovery() {
        MemoryBackend backend = new MemoryBackend();
        backend.putString("config_json", "{not-json");
        AppSupervisionStore store = new AppSupervisionStore(backend);

        AppSupervisionStore.ConfigSnapshot loaded = store.loadConfig();

        assertFalse(loaded.isFeatureEnabled());
        assertTrue(loaded.getGroups().isEmpty());
        List<AppSupervisionStore.LogRecord> logs = store.readLogs(System.currentTimeMillis());
        assertEquals(1, logs.size());
        assertEquals("storage_recovered", logs.get(0).getType());
    }

    private static AppSupervisionStore.LogRecord logAt(long wallMs) {
        return new AppSupervisionStore.LogRecord(
                wallMs, "diagnostic", "group-1", "保留 Unicode");
    }

    private static final class MemoryBackend implements AppSupervisionStore.Backend {
        private final Map<String, String> values = new LinkedHashMap<>();

        @Override
        public String getString(String key, String defaultValue) {
            String value = values.get(key);
            return value == null ? defaultValue : value;
        }

        @Override
        public void putString(String key, String value) {
            values.put(key, value);
        }
    }
}
