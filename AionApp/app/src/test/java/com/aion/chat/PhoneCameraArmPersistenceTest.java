package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.HashMap;
import java.util.Map;

public class PhoneCameraArmPersistenceTest {
    @Test
    public void armedConfigurationRestoresIntoFreshServiceState() {
        MemoryStore store = new MemoryStore();
        PhoneCameraArmPersistence first = new PhoneCameraArmPersistence(store);
        first.rememberArmed("front", 2f);

        PhoneCameraState restartedState = new PhoneCameraState();
        PhoneCameraArmPersistence restarted =
                new PhoneCameraArmPersistence(store);
        restarted.restoreInto(restartedState);

        assertTrue(restartedState.isArmed());
        assertEquals("front", restartedState.getFacing());
        assertEquals(2f, restartedState.getZoom(), 0.001f);
    }

    @Test
    public void explicitDisarmSurvivesFreshServiceState() {
        MemoryStore store = new MemoryStore();
        PhoneCameraArmPersistence persistence =
                new PhoneCameraArmPersistence(store);
        persistence.rememberArmed("front", 2f);
        persistence.rememberDisarmed();

        PhoneCameraState restartedState = new PhoneCameraState();
        new PhoneCameraArmPersistence(store).restoreInto(restartedState);

        assertFalse(restartedState.isArmed());
    }

    private static final class MemoryStore
            implements PhoneCameraArmPersistence.Store {
        private final Map<String, Object> values = new HashMap<>();

        @Override
        public boolean getBoolean(String key, boolean fallback) {
            Object value = values.get(key);
            return value instanceof Boolean ? (Boolean) value : fallback;
        }

        @Override
        public String getString(String key, String fallback) {
            Object value = values.get(key);
            return value instanceof String ? (String) value : fallback;
        }

        @Override
        public float getFloat(String key, float fallback) {
            Object value = values.get(key);
            return value instanceof Float ? (Float) value : fallback;
        }

        @Override
        public void put(boolean armed, String facing, float zoom) {
            values.put("armed", armed);
            values.put("facing", facing);
            values.put("zoom", zoom);
        }
    }
}
