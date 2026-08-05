package com.aion.chat;

final class PhoneCameraArmPersistence {
    interface Store {
        boolean getBoolean(String key, boolean fallback);
        String getString(String key, String fallback);
        float getFloat(String key, float fallback);
        void put(boolean armed, String facing, float zoom);
    }

    private static final String KEY_ARMED = "armed";
    private static final String KEY_FACING = "facing";
    private static final String KEY_ZOOM = "zoom";

    private final Store store;

    PhoneCameraArmPersistence(Store store) {
        this.store = store;
    }

    void restoreInto(PhoneCameraState state) {
        if (!store.getBoolean(KEY_ARMED, false)) {
            state.disarm();
            return;
        }
        state.arm(
                store.getString(KEY_FACING, "back"),
                store.getFloat(KEY_ZOOM, 1f)
        );
    }

    void rememberArmed(String facing, float zoom) {
        store.put(
                true,
                PhoneCameraImagePolicy.normalizeFacing(facing),
                Math.max(0.1f, zoom)
        );
    }

    void rememberDisarmed() {
        store.put(false, "back", 1f);
    }
}
