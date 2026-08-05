package com.aion.chat.homecoming;

import android.app.AppOpsManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Process;

import com.aion.chat.AionAccessibilityService;
import com.aion.chat.supervision.AppGroup;
import com.aion.chat.supervision.AppSupervisionRuntime;

import org.json.JSONArray;
import org.json.JSONObject;

public final class HomecomingSupervisionAdapter {
    private final RuntimePort runtime;
    private final PermissionPort permissions;
    private final CommandPort commands;

    public HomecomingSupervisionAdapter(Context context) {
        this(new AndroidRuntimePort(context), new AndroidPermissionPort(context));
    }

    HomecomingSupervisionAdapter(
            RuntimePort runtime, PermissionPort permissions) {
        this(
                runtime,
                permissions,
                runtime instanceof CommandPort
                        ? (CommandPort) runtime
                        : (action, groupId, minutes, roleId, message,
                                commandId, expiresWallMs) ->
                                new HomecomingSupervisionCommandHandler.RuntimeResult(
                                        false, "execution_failed", ""));
    }

    HomecomingSupervisionAdapter(
            RuntimePort runtime,
            PermissionPort permissions,
            CommandPort commands) {
        if (runtime == null) throw new IllegalArgumentException("runtime is required");
        if (permissions == null) {
            throw new IllegalArgumentException("permissions are required");
        }
        if (commands == null) throw new IllegalArgumentException("commands are required");
        this.runtime = runtime;
        this.permissions = permissions;
        this.commands = commands;
    }

    public Snapshot snapshot() {
        try {
            boolean enabled = runtime.enabled();
            String readiness = normalizeReadiness(permissions.readiness());
            JSONObject state = runtime.state();
            return new Snapshot(
                    enabled,
                    readiness,
                    state == null ? new JSONObject()
                            : new JSONObject(state.toString()));
        } catch (Exception exception) {
            return new Snapshot(false, "unavailable", new JSONObject());
        }
    }

    public HomecomingSupervisionCommandHandler.RuntimeResult applyCommand(
            String action,
            String groupId,
            int minutes,
            String roleId,
            String message,
            String commandId,
            long expiresWallMs) {
        try {
            return commands.apply(
                    action, groupId, minutes, roleId, message,
                    commandId, expiresWallMs);
        } catch (Exception exception) {
            return new HomecomingSupervisionCommandHandler.RuntimeResult(
                    false, "execution_failed", "");
        }
    }

    interface RuntimePort {
        boolean enabled();
        JSONObject state() throws Exception;
    }

    interface PermissionPort {
        String readiness();
    }

    interface CommandPort {
        HomecomingSupervisionCommandHandler.RuntimeResult apply(
                String action,
                String groupId,
                int minutes,
                String roleId,
                String message,
                String commandId,
                long expiresWallMs);
    }

    interface SnapshotSource {
        Snapshot snapshot();
    }

    public static final class Snapshot {
        public final boolean enabled;
        public final String readiness;
        public final JSONObject state;

        Snapshot(boolean enabled, String readiness, JSONObject state) {
            this.enabled = enabled;
            this.readiness = normalizeReadiness(readiness);
            this.state = state == null ? new JSONObject() : state;
        }
    }

    private static final class AndroidRuntimePort
            implements RuntimePort, CommandPort {
        final AppSupervisionRuntime runtime;

        AndroidRuntimePort(Context context) {
            if (context == null) throw new IllegalArgumentException("context is required");
            runtime = AppSupervisionRuntime.start(context.getApplicationContext());
        }

        @Override
        public boolean enabled() {
            return runtime.engine().isFeatureEnabled();
        }

        @Override
        public JSONObject state() throws Exception {
            JSONObject state = runtime.buildStatePayload("snapshot", "", 0L);
            JSONArray groups = state.optJSONArray("groups");
            if (groups == null) return state;
            for (int index = 0; index < groups.length(); index++) {
                JSONObject projected = groups.optJSONObject(index);
                if (projected == null) continue;
                AppGroup configured = runtime.engine().group(
                        projected.optString("groupId", ""));
                JSONArray checkpoints = new JSONArray();
                if (configured != null) {
                    for (Long checkpoint :
                            configured.getPolicy().getCheckpointsMs()) {
                        checkpoints.put(checkpoint / 60_000L);
                    }
                }
                projected.put("checkpointsMinutes", checkpoints);
            }
            return state;
        }

        @Override
        public HomecomingSupervisionCommandHandler.RuntimeResult apply(
                String action,
                String groupId,
                int minutes,
                String roleId,
                String message,
                String commandId,
                long expiresWallMs) {
            AppGroup group = runtime.engine().group(groupId);
            String displayName = group == null ? "" : group.getDisplayName();
            AppSupervisionRuntime.CommandResult result = runtime.applyAiCommand(
                    action,
                    groupId,
                    minutes,
                    roleId,
                    message,
                    commandId,
                    expiresWallMs);
            return new HomecomingSupervisionCommandHandler.RuntimeResult(
                    result.isSuccess(), result.getReason(), displayName);
        }
    }

    private static final class AndroidPermissionPort implements PermissionPort {
        private final Context context;

        AndroidPermissionPort(Context context) {
            if (context == null) throw new IllegalArgumentException("context is required");
            this.context = context.getApplicationContext();
        }

        @Override
        public String readiness() {
            boolean accessibility = AionAccessibilityService.isEnabledInSettings(context);
            boolean usage = hasUsageAccess(context);
            if (accessibility && usage) return "ready";
            if (accessibility || usage) return "degraded";
            return "unavailable";
        }

        private static boolean hasUsageAccess(Context context) {
            try {
                AppOpsManager operations =
                        (AppOpsManager) context.getSystemService(Context.APP_OPS_SERVICE);
                if (operations == null) return false;
                int mode = operations.checkOpNoThrow(
                        AppOpsManager.OPSTR_GET_USAGE_STATS,
                        Process.myUid(),
                        context.getPackageName());
                if (mode == AppOpsManager.MODE_ALLOWED) return true;
                return context.checkSelfPermission(
                        android.Manifest.permission.PACKAGE_USAGE_STATS)
                        == PackageManager.PERMISSION_GRANTED;
            } catch (Exception exception) {
                return false;
            }
        }
    }

    private static String normalizeReadiness(String value) {
        if ("ready".equals(value) || "degraded".equals(value)
                || "unavailable".equals(value)) {
            return value;
        }
        return "unavailable";
    }
}
