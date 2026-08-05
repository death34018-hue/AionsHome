package com.aion.chat.homecoming;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.UUID;

public final class HomecomingModeStore {
    public static final String PREFERENCES_NAME = "homecoming_prefs";

    static final String KEY_MODE = "mode";
    static final String KEY_EPOCH = "epoch";
    static final String KEY_ACTIVATED_AT = "activated_at";
    static final String KEY_PENDING_IMPORT = "pending_import_path";
    static final String KEY_PENDING_PACKAGE = "pending_return_package_id";

    private static final String MODE_INACTIVE = "inactive";
    private static final String MODE_ACTIVE = "active";
    private static final String MODE_FREEZING = "freezing";
    private static final String MODE_FROZEN = "frozen";
    private static final String MODE_RETURNING = "returning";

    private final Backend backend;

    public HomecomingModeStore(Context context) {
        this(new SharedPreferencesBackend(context.getSharedPreferences(
                PREFERENCES_NAME, Context.MODE_PRIVATE)));
    }

    HomecomingModeStore(Backend backend) {
        if (backend == null) {
            throw new IllegalArgumentException("backend is required");
        }
        this.backend = backend;
    }

    public boolean isActive() {
        return MODE_ACTIVE.equals(backend.getString(KEY_MODE, MODE_INACTIVE));
    }

    public boolean isFrozen() {
        String mode = backend.getString(KEY_MODE, MODE_INACTIVE);
        return MODE_FROZEN.equals(mode) || MODE_RETURNING.equals(mode);
    }

    public boolean isFreezing() {
        return MODE_FREEZING.equals(
                backend.getString(KEY_MODE, MODE_INACTIVE));
    }

    public boolean isReturning() {
        return MODE_RETURNING.equals(
                backend.getString(KEY_MODE, MODE_INACTIVE));
    }

    public String activate() {
        String epoch = UUID.randomUUID().toString();
        activate(epoch, System.currentTimeMillis());
        return epoch;
    }

    void activate(String epoch, long activatedAt) {
        if (epoch == null || epoch.trim().isEmpty()) {
            throw new IllegalArgumentException("epoch is required");
        }
        backend.putString(KEY_EPOCH, epoch.trim());
        backend.putLong(KEY_ACTIVATED_AT, activatedAt);
        backend.putString(KEY_MODE, MODE_ACTIVE);
    }

    public void freeze() {
        if (!currentEpoch().isEmpty()) {
            backend.putString(KEY_MODE, MODE_FROZEN);
        }
    }

    public void beginFreezing() {
        if (!isActive() || currentEpoch().isEmpty()) {
            throw new IllegalStateException("active Homecoming epoch is required");
        }
        backend.putString(KEY_MODE, MODE_FREEZING);
    }

    public void resumeActive() {
        if (!currentEpoch().isEmpty()) {
            backend.putString(KEY_MODE, MODE_ACTIVE);
        }
    }

    public void markFrozen(String packageId) {
        setPendingPackageId(packageId);
        backend.putString(KEY_MODE, MODE_FROZEN);
    }

    public void markReturning(String packageId) {
        setPendingPackageId(packageId);
        backend.putString(KEY_MODE, MODE_RETURNING);
    }

    public void deactivateAfterPackageSaved() {
        backend.putString(KEY_MODE, MODE_INACTIVE);
        backend.remove(KEY_EPOCH);
        backend.remove(KEY_ACTIVATED_AT);
    }

    public void deactivateAfterSuccessfulReturn() {
        deactivateAfterPackageSaved();
        backend.remove(KEY_PENDING_PACKAGE);
        backend.remove(KEY_PENDING_IMPORT);
    }

    public String currentEpoch() {
        return backend.getString(KEY_EPOCH, "");
    }

    public long activatedAt() {
        return backend.getLong(KEY_ACTIVATED_AT, 0L);
    }

    public String pendingImportPath() {
        return backend.getString(KEY_PENDING_IMPORT, "");
    }

    public String pendingPackageId() {
        return backend.getString(KEY_PENDING_PACKAGE, "");
    }

    public void setPendingImportPath(String path) {
        if (path == null || path.trim().isEmpty()) {
            backend.remove(KEY_PENDING_IMPORT);
        } else {
            backend.putString(KEY_PENDING_IMPORT, path.trim());
        }
    }

    private void setPendingPackageId(String packageId) {
        if (packageId == null || packageId.trim().isEmpty()) {
            throw new IllegalArgumentException("packageId is required");
        }
        backend.putString(KEY_PENDING_PACKAGE, packageId.trim());
    }

    interface Backend {
        String getString(String key, String defaultValue);
        long getLong(String key, long defaultValue);
        void putString(String key, String value);
        void putLong(String key, long value);
        void remove(String key);
    }

    private static final class SharedPreferencesBackend implements Backend {
        private final SharedPreferences preferences;

        SharedPreferencesBackend(SharedPreferences preferences) {
            this.preferences = preferences;
        }

        @Override
        public String getString(String key, String defaultValue) {
            return preferences.getString(key, defaultValue);
        }

        @Override
        public long getLong(String key, long defaultValue) {
            return preferences.getLong(key, defaultValue);
        }

        @Override
        public void putString(String key, String value) {
            preferences.edit().putString(key, value).apply();
        }

        @Override
        public void putLong(String key, long value) {
            preferences.edit().putLong(key, value).apply();
        }

        @Override
        public void remove(String key) {
            preferences.edit().remove(key).apply();
        }
    }
}
