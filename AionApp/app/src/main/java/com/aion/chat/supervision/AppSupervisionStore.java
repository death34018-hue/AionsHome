package com.aion.chat.supervision;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class AppSupervisionStore {
    static final String PREFS_NAME = "app_supervision_v1";
    private static final String CONFIG_JSON = "config_json";
    private static final String RUNTIME_JSON = "runtime_json";
    private static final String RUNTIME_BOOT_ID = "runtime_boot_id";
    private static final String LOGS_JSON = "logs_json";
    private static final int SCHEMA_VERSION = 1;
    private static final int MAX_LOGS = 500;
    private static final long LOG_RETENTION_MS = 48L * 60L * 60L * 1000L;

    private final Backend backend;

    public AppSupervisionStore(Context context) {
        if (context == null) {
            throw new IllegalArgumentException("context is required");
        }
        this.backend = new SharedPreferencesBackend(context.getApplicationContext()
                .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE));
    }

    AppSupervisionStore(Backend backend) {
        if (backend == null) {
            throw new IllegalArgumentException("backend is required");
        }
        this.backend = backend;
    }

    public synchronized ConfigSnapshot loadConfig() {
        String raw = backend.getString(CONFIG_JSON, "");
        if (raw.isEmpty()) {
            return ConfigSnapshot.defaults();
        }
        try {
            JSONObject root = requireSchema(new JSONObject(raw));
            JSONArray groupValues = root.getJSONArray("groups");
            ArrayList<AppGroup> groups = new ArrayList<>();
            for (int i = 0; i < groupValues.length(); i++) {
                groups.add(groupFromJson(groupValues.getJSONObject(i)));
            }
            return new ConfigSnapshot(root.optBoolean("feature_enabled", false), groups);
        } catch (Exception exception) {
            backend.putString(CONFIG_JSON, "");
            recordRecovery("config_json");
            return ConfigSnapshot.defaults();
        }
    }

    public synchronized void saveConfig(ConfigSnapshot snapshot) {
        if (snapshot == null) {
            throw new IllegalArgumentException("snapshot is required");
        }
        try {
            JSONObject root = root();
            root.put("feature_enabled", snapshot.isFeatureEnabled());
            JSONArray groups = new JSONArray();
            for (AppGroup group : snapshot.getGroups()) {
                groups.put(groupToJson(group));
            }
            root.put("groups", groups);
            backend.putString(CONFIG_JSON, root.toString());
        } catch (Exception exception) {
            throw new IllegalStateException("config serialization failed", exception);
        }
    }

    public synchronized RuntimeSnapshot loadBootScopedState(String bootId) {
        String checkedBootId = required(bootId, "bootId");
        String storedBootId = backend.getString(RUNTIME_BOOT_ID, "");
        if (!checkedBootId.equals(storedBootId)) {
            backend.putString(RUNTIME_BOOT_ID, checkedBootId);
            backend.putString(RUNTIME_JSON, "");
            return RuntimeSnapshot.empty();
        }
        String raw = backend.getString(RUNTIME_JSON, "");
        if (raw.isEmpty()) {
            return RuntimeSnapshot.empty();
        }
        try {
            JSONObject root = requireSchema(new JSONObject(raw));
            JSONObject stateValues = root.getJSONObject("states");
            LinkedHashMap<String, PersistedGroupState> states = new LinkedHashMap<>();
            JSONArray names = stateValues.names();
            if (names != null) {
                for (int i = 0; i < names.length(); i++) {
                    String groupId = names.getString(i);
                    states.put(groupId, stateFromJson(stateValues.getJSONObject(groupId)));
                }
            }
            JSONObject deviceValue = root.optJSONObject("device_state");
            PersistedDeviceState deviceState = deviceValue == null
                    ? PersistedDeviceState.empty()
                    : deviceStateFromJson(deviceValue);
            return new RuntimeSnapshot(states, deviceState);
        } catch (Exception exception) {
            backend.putString(RUNTIME_JSON, "");
            recordRecovery("runtime_json");
            return RuntimeSnapshot.empty();
        }
    }

    public synchronized void saveBootScopedState(String bootId, RuntimeSnapshot snapshot) {
        String checkedBootId = required(bootId, "bootId");
        if (snapshot == null) {
            throw new IllegalArgumentException("snapshot is required");
        }
        try {
            JSONObject root = root();
            JSONObject states = new JSONObject();
            for (Map.Entry<String, PersistedGroupState> entry : snapshot.getStates().entrySet()) {
                states.put(entry.getKey(), stateToJson(entry.getValue()));
            }
            root.put("states", states);
            root.put("device_state", deviceStateToJson(snapshot.getDeviceState()));
            backend.putString(RUNTIME_BOOT_ID, checkedBootId);
            backend.putString(RUNTIME_JSON, root.toString());
        } catch (Exception exception) {
            throw new IllegalStateException("runtime serialization failed", exception);
        }
    }

    public synchronized void appendLog(LogRecord record) {
        if (record == null) {
            throw new IllegalArgumentException("record is required");
        }
        ArrayList<LogRecord> logs = readLogsInternal();
        logs.add(record);
        trimToCap(logs);
        writeLogs(logs);
    }

    public synchronized List<LogRecord> readLogs(long nowWallMs) {
        ArrayList<LogRecord> logs = readLogsInternal();
        long cutoff = nowWallMs - LOG_RETENTION_MS;
        for (int i = logs.size() - 1; i >= 0; i--) {
            if (logs.get(i).getWallMs() < cutoff) {
                logs.remove(i);
            }
        }
        trimToCap(logs);
        writeLogs(logs);
        return Collections.unmodifiableList(new ArrayList<>(logs));
    }

    private ArrayList<LogRecord> readLogsInternal() {
        String raw = backend.getString(LOGS_JSON, "");
        ArrayList<LogRecord> logs = new ArrayList<>();
        if (raw.isEmpty()) {
            return logs;
        }
        try {
            JSONObject root = requireSchema(new JSONObject(raw));
            JSONArray values = root.getJSONArray("items");
            for (int i = 0; i < values.length(); i++) {
                JSONObject value = values.getJSONObject(i);
                logs.add(new LogRecord(
                        value.getLong("wall_ms"),
                        value.getString("type"),
                        value.optString("group_id", ""),
                        value.optString("message", "")));
            }
            return logs;
        } catch (Exception exception) {
            backend.putString(LOGS_JSON, "");
            logs.add(new LogRecord(
                    System.currentTimeMillis(), "storage_recovered", "", "logs_json"));
            writeLogs(logs);
            return logs;
        }
    }

    private void recordRecovery(String key) {
        appendLog(new LogRecord(
                System.currentTimeMillis(), "storage_recovered", "", key));
    }

    private void writeLogs(List<LogRecord> logs) {
        try {
            JSONObject root = root();
            JSONArray items = new JSONArray();
            for (LogRecord log : logs) {
                JSONObject value = new JSONObject();
                value.put("wall_ms", log.getWallMs());
                value.put("type", log.getType());
                value.put("group_id", log.getGroupId());
                value.put("message", log.getMessage());
                items.put(value);
            }
            root.put("items", items);
            backend.putString(LOGS_JSON, root.toString());
        } catch (Exception exception) {
            throw new IllegalStateException("log serialization failed", exception);
        }
    }

    private static void trimToCap(List<LogRecord> logs) {
        while (logs.size() > MAX_LOGS) {
            logs.remove(0);
        }
    }

    private static JSONObject groupToJson(AppGroup group) throws Exception {
        JSONObject value = new JSONObject();
        value.put("group_id", group.getGroupId());
        value.put("display_name", group.getDisplayName());
        value.put("monitored", group.isMonitored());
        value.put("role_id", group.getPolicy().getRoleId());
        value.put("idle_reset_ms", group.getPolicy().getIdleResetMs());
        value.put("packages", new JSONArray(group.getPackageNames()));
        value.put("checkpoints_ms", new JSONArray(group.getPolicy().getCheckpointsMs()));
        return value;
    }

    private static AppGroup groupFromJson(JSONObject value) throws Exception {
        ArrayList<String> packages = new ArrayList<>();
        JSONArray packageValues = value.getJSONArray("packages");
        for (int i = 0; i < packageValues.length(); i++) {
            packages.add(packageValues.getString(i));
        }
        ArrayList<Long> checkpoints = new ArrayList<>();
        JSONArray checkpointValues = value.getJSONArray("checkpoints_ms");
        for (int i = 0; i < checkpointValues.length(); i++) {
            checkpoints.add(checkpointValues.getLong(i));
        }
        SupervisionPolicy policy = SupervisionPolicy.of(
                value.getLong("idle_reset_ms"), checkpoints, value.getString("role_id"));
        return AppGroup.create(
                value.getString("group_id"),
                value.getString("display_name"),
                packages,
                value.optBoolean("monitored", true),
                policy);
    }

    private static JSONObject stateToJson(PersistedGroupState state) throws Exception {
        JSONObject value = new JSONObject();
        value.put("round_usage_ms", state.getRoundUsageMs());
        if (state.getLastExitElapsedMs() != null) {
            value.put("last_exit_elapsed_ms", state.getLastExitElapsedMs());
        }
        value.put("fired_checkpoints_ms", new JSONArray(state.getFiredCheckpointsMs()));
        if (state.getLock() != null) {
            value.put("lock", directiveToJson(state.getLock()));
        }
        if (state.getTemporaryUnlock() != null) {
            value.put("temporary_unlock", directiveToJson(state.getTemporaryUnlock()));
        }
        return value;
    }

    private static PersistedGroupState stateFromJson(JSONObject value) throws Exception {
        LinkedHashSet<Long> checkpoints = new LinkedHashSet<>();
        JSONArray checkpointValues = value.getJSONArray("fired_checkpoints_ms");
        for (int i = 0; i < checkpointValues.length(); i++) {
            checkpoints.add(checkpointValues.getLong(i));
        }
        return new PersistedGroupState(
                value.getLong("round_usage_ms"),
                value.has("last_exit_elapsed_ms")
                        ? value.getLong("last_exit_elapsed_ms") : null,
                checkpoints,
                value.has("lock") ? directiveFromJson(value.getJSONObject("lock")) : null,
                value.has("temporary_unlock")
                        ? directiveFromJson(value.getJSONObject("temporary_unlock")) : null);
    }

    private static JSONObject deviceStateToJson(PersistedDeviceState state)
            throws Exception {
        JSONObject value = new JSONObject();
        if (state.getLock() != null) {
            value.put("lock", directiveToJson(state.getLock()));
        }
        if (state.getTemporaryUnlock() != null) {
            value.put(
                    "temporary_unlock",
                    directiveToJson(state.getTemporaryUnlock()));
        }
        return value;
    }

    private static PersistedDeviceState deviceStateFromJson(JSONObject value)
            throws Exception {
        return new PersistedDeviceState(
                value.has("lock")
                        ? directiveFromJson(value.getJSONObject("lock")) : null,
                value.has("temporary_unlock")
                        ? directiveFromJson(value.getJSONObject("temporary_unlock"))
                        : null);
    }

    private static JSONObject directiveToJson(TimedDirective directive) throws Exception {
        JSONObject value = new JSONObject();
        value.put("received_elapsed_ms", directive.getReceivedElapsedMs());
        value.put("received_wall_ms", directive.getReceivedWallMs());
        value.put("duration_ms", directive.getDurationMs());
        value.put("role_id", directive.getRoleId());
        value.put("message", directive.getMessage());
        value.put("command_id", directive.getCommandId());
        return value;
    }

    private static TimedDirective directiveFromJson(JSONObject value) throws Exception {
        long durationMs = value.getLong("duration_ms");
        if (durationMs % 60_000L != 0) {
            throw new IllegalArgumentException("directive duration must use whole minutes");
        }
        return TimedDirective.create(
                value.getLong("received_elapsed_ms"),
                value.getLong("received_wall_ms"),
                (int) (durationMs / 60_000L),
                value.getString("role_id"),
                value.getString("message"),
                value.getString("command_id"));
    }

    private static JSONObject root() throws Exception {
        JSONObject root = new JSONObject();
        root.put("schema_version", SCHEMA_VERSION);
        return root;
    }

    private static JSONObject requireSchema(JSONObject root) throws Exception {
        if (root.getInt("schema_version") != SCHEMA_VERSION) {
            throw new IllegalArgumentException("unsupported schema");
        }
        return root;
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }

    interface Backend {
        String getString(String key, String defaultValue);
        void putString(String key, String value);
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
        public void putString(String key, String value) {
            preferences.edit().putString(key, value).apply();
        }
    }

    public static final class ConfigSnapshot {
        private final boolean featureEnabled;
        private final List<AppGroup> groups;

        public ConfigSnapshot(boolean featureEnabled, List<AppGroup> groups) {
            if (groups == null) {
                throw new IllegalArgumentException("groups is required");
            }
            this.featureEnabled = featureEnabled;
            this.groups = Collections.unmodifiableList(new ArrayList<>(groups));
        }

        public static ConfigSnapshot defaults() {
            return new ConfigSnapshot(false, Collections.<AppGroup>emptyList());
        }

        public boolean isFeatureEnabled() { return featureEnabled; }
        public List<AppGroup> getGroups() { return groups; }
    }

    public static final class RuntimeSnapshot {
        private final Map<String, PersistedGroupState> states;
        private final PersistedDeviceState deviceState;

        public RuntimeSnapshot(Map<String, PersistedGroupState> states) {
            this(states, PersistedDeviceState.empty());
        }

        public RuntimeSnapshot(
                Map<String, PersistedGroupState> states,
                PersistedDeviceState deviceState) {
            if (states == null) {
                throw new IllegalArgumentException("states is required");
            }
            if (deviceState == null) {
                throw new IllegalArgumentException("deviceState is required");
            }
            this.states = Collections.unmodifiableMap(new LinkedHashMap<>(states));
            this.deviceState = deviceState;
        }

        public static RuntimeSnapshot empty() {
            return new RuntimeSnapshot(
                    Collections.<String, PersistedGroupState>emptyMap(),
                    PersistedDeviceState.empty());
        }

        public Map<String, PersistedGroupState> getStates() { return states; }
        public PersistedDeviceState getDeviceState() { return deviceState; }
    }

    public static final class PersistedDeviceState {
        private final TimedDirective lock;
        private final TimedDirective temporaryUnlock;

        public PersistedDeviceState(
                TimedDirective lock,
                TimedDirective temporaryUnlock) {
            this.lock = lock;
            this.temporaryUnlock = temporaryUnlock;
        }

        public static PersistedDeviceState empty() {
            return new PersistedDeviceState(null, null);
        }

        public TimedDirective getLock() { return lock; }
        public TimedDirective getTemporaryUnlock() { return temporaryUnlock; }
    }

    public static final class PersistedGroupState {
        private final long roundUsageMs;
        private final Long lastExitElapsedMs;
        private final Set<Long> firedCheckpointsMs;
        private final TimedDirective lock;
        private final TimedDirective temporaryUnlock;

        public PersistedGroupState(long roundUsageMs, Long lastExitElapsedMs,
                Set<Long> firedCheckpointsMs, TimedDirective lock,
                TimedDirective temporaryUnlock) {
            if (roundUsageMs < 0 || firedCheckpointsMs == null) {
                throw new IllegalArgumentException("invalid persisted state");
            }
            this.roundUsageMs = roundUsageMs;
            this.lastExitElapsedMs = lastExitElapsedMs;
            this.firedCheckpointsMs = Collections.unmodifiableSet(
                    new LinkedHashSet<>(firedCheckpointsMs));
            this.lock = lock;
            this.temporaryUnlock = temporaryUnlock;
        }

        public long getRoundUsageMs() { return roundUsageMs; }
        public Long getLastExitElapsedMs() { return lastExitElapsedMs; }
        public Set<Long> getFiredCheckpointsMs() { return firedCheckpointsMs; }
        public TimedDirective getLock() { return lock; }
        public TimedDirective getTemporaryUnlock() { return temporaryUnlock; }
    }

    public static final class LogRecord {
        private final long wallMs;
        private final String type;
        private final String groupId;
        private final String message;

        public LogRecord(long wallMs, String type, String groupId, String message) {
            if (wallMs < 0) {
                throw new IllegalArgumentException("wallMs must be nonnegative");
            }
            this.wallMs = wallMs;
            this.type = required(type, "type");
            this.groupId = groupId == null ? "" : groupId;
            this.message = message == null ? "" : message;
        }

        public long getWallMs() { return wallMs; }
        public String getType() { return type; }
        public String getGroupId() { return groupId; }
        public String getMessage() { return message; }
    }
}
