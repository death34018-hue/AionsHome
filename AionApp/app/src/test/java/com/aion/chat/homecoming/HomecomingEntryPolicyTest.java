package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.LinkedHashMap;
import java.util.Map;

import static com.aion.chat.homecoming.HomecomingEntryPolicy.Destination.ADDRESS_PICKER;
import static com.aion.chat.homecoming.HomecomingEntryPolicy.Destination.HOMECOMING;
import static com.aion.chat.homecoming.HomecomingEntryPolicy.Destination.NORMAL_RESUME;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingEntryPolicyTest {
    @Test
    public void activeHomecomingAlwaysResumesHomecomingUntilManualReturn() {
        assertEquals(HOMECOMING, HomecomingEntryPolicy.destination(true, false, true));
        assertEquals(HOMECOMING, HomecomingEntryPolicy.destination(true, true, false));
    }

    @Test
    public void forceAddressPickerDoesNotSilentlyActivateHomecoming() {
        assertEquals(ADDRESS_PICKER, HomecomingEntryPolicy.destination(false, true, false));
    }

    @Test
    public void normalLaunchKeepsExistingResumeBehavior() {
        assertEquals(NORMAL_RESUME, HomecomingEntryPolicy.destination(false, false, true));
    }

    @Test
    public void modeTransitionsAreExplicitAndKeepEpochUntilPackageIsSaved() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingModeStore store = new HomecomingModeStore(backend);

        assertFalse(store.isActive());
        store.activate("epoch-test", 1234L);
        assertTrue(store.isActive());
        assertEquals("epoch-test", store.currentEpoch());
        assertEquals(1234L, store.activatedAt());

        store.beginFreezing();
        assertTrue(store.isFreezing());
        store.markFrozen("package-one");
        assertTrue(store.isFrozen());
        assertEquals("epoch-test", store.currentEpoch());
        assertEquals("package-one", store.pendingPackageId());

        store.markReturning("package-one");
        assertTrue(store.isReturning());
        assertTrue(store.isFrozen());

        store.setPendingImportPath("/safe/homecoming-return.json");
        store.deactivateAfterPackageSaved();
        assertFalse(store.isActive());
        assertFalse(store.isFrozen());
        assertEquals("", store.currentEpoch());
        assertEquals("/safe/homecoming-return.json", store.pendingImportPath());
    }

    @Test
    public void activationCannotTouchNormalPreferences() {
        MemoryBackend normal = new MemoryBackend();
        normal.putString("saved_url", "https://normal.example/chat");
        normal.putBoolean("auto_connect", true);

        HomecomingModeStore store = new HomecomingModeStore(new MemoryBackend());
        store.activate("epoch-test", 10L);

        assertEquals("https://normal.example/chat", normal.getString("saved_url", ""));
        assertTrue(normal.getBoolean("auto_connect", false));
    }

    @Test
    public void activityReadinessUsesImportedMetadataWithoutInflatingSnapshot()
            throws Exception {
        String source = new String(
                Files.readAllBytes(Paths.get(
                        "src/main/java/com/aion/chat/homecoming/"
                                + "HomecomingActivity.java")),
                StandardCharsets.UTF_8);

        assertTrue(source.contains("metaString(readable, \"snapshot_id\")"));
        assertFalse(source.contains("activeManifest()"));
    }

    private static final class MemoryBackend implements HomecomingModeStore.Backend {
        private final Map<String, Object> values = new LinkedHashMap<>();

        @Override
        public String getString(String key, String defaultValue) {
            Object value = values.get(key);
            return value instanceof String ? (String) value : defaultValue;
        }

        @Override
        public long getLong(String key, long defaultValue) {
            Object value = values.get(key);
            return value instanceof Long ? (Long) value : defaultValue;
        }

        @Override
        public void putString(String key, String value) {
            values.put(key, value);
        }

        @Override
        public void putLong(String key, long value) {
            values.put(key, value);
        }

        @Override
        public void remove(String key) {
            values.remove(key);
        }

        void putBoolean(String key, boolean value) {
            values.put(key, value);
        }

        boolean getBoolean(String key, boolean defaultValue) {
            Object value = values.get(key);
            return value instanceof Boolean ? (Boolean) value : defaultValue;
        }
    }
}
