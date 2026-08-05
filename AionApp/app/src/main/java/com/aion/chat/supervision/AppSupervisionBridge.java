package com.aion.chat.supervision;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.net.Uri;
import android.provider.Settings;
import android.webkit.JavascriptInterface;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class AppSupervisionBridge {
    private final Context context;
    private final AppSupervisionRuntime runtime;

    public AppSupervisionBridge(Context context, AppSupervisionRuntime runtime) {
        if (runtime == null) throw new IllegalArgumentException("runtime is required");
        this.context = context;
        this.runtime = runtime;
    }

    @JavascriptInterface
    public String getSnapshot() {
        try {
            runtime.settleIdleRounds();
            JSONObject out = ok();
            out.put("featureEnabled", runtime.engine().isFeatureEnabled());
            out.put("recoveryState", runtime.recoveryState().name());
            out.put("recoveryDiagnostic", runtime.recoveryDiagnostic());
            JSONArray groups = new JSONArray();
            long now = runtime.elapsedRealtime();
            for (AppGroup group : runtime.engine().groups()) {
                AppGroupState.Snapshot state = runtime.engine().snapshot(group.getGroupId(), now);
                JSONObject item = groupJson(group);
                item.put("effectiveState", runtime.engine()
                        .effectiveState(group.getGroupId(), now).name());
                item.put("roundUsageMs", state.getRoundUsageMs());
                item.put("foregroundOpen", state.isForegroundOpen());
                item.put("firedCheckpointsMs", new JSONArray(state.getFiredCheckpointsMs()));
                item.put("lock", directiveJson(state.getLock()));
                item.put("temporaryUnlock", directiveJson(state.getTemporaryUnlock()));
                groups.put(item);
            }
            out.put("groups", groups);
            DeviceLockState.Snapshot device = runtime.deviceSnapshot();
            JSONObject deviceJson = new JSONObject();
            deviceJson.put("effectiveState", device.getEffectiveState().name());
            deviceJson.put("lock", directiveJson(device.getLock()));
            deviceJson.put("temporaryUnlock",
                    directiveJson(device.getTemporaryUnlock()));
            out.put("deviceLock", deviceJson);
            JSONArray logs = new JSONArray();
            for (AppSupervisionStore.LogRecord record : runtime.logs()) {
                JSONObject item = new JSONObject();
                item.put("wallMs", record.getWallMs());
                item.put("type", record.getType());
                item.put("groupId", record.getGroupId());
                item.put("message", record.getMessage());
                logs.put(item);
            }
            out.put("logs", logs);
            return out.toString();
        } catch (Exception exception) {
            return error(exception);
        }
    }

    @JavascriptInterface
    public String listLaunchableApps() {
        try {
            JSONObject out = ok();
            JSONArray apps = new JSONArray();
            if (context != null) {
                PackageManager manager = context.getPackageManager();
                Intent intent = new Intent(Intent.ACTION_MAIN);
                intent.addCategory(Intent.CATEGORY_LAUNCHER);
                List<ResolveInfo> resolved = manager.queryIntentActivities(intent, 0);
                for (ResolveInfo info : resolved) {
                    String packageName = info.activityInfo.packageName;
                    if (runtime.isProtectedPackage(packageName)) continue;
                    JSONObject app = new JSONObject();
                    app.put("packageName", packageName);
                    app.put("label", String.valueOf(info.loadLabel(manager)));
                    apps.put(app);
                }
            }
            out.put("apps", apps);
            return out.toString();
        } catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface
    public String setRoleCatalog(String json) {
        try {
            JSONArray roles = new JSONObject(json).getJSONArray("roles");
            Map<String, String> labels = new LinkedHashMap<>();
            for (int i = 0; i < roles.length(); i++) {
                JSONObject role = roles.getJSONObject(i);
                labels.put(role.getString("id"), role.getString("label"));
            }
            runtime.setRoleLabels(labels);
            return ok().toString();
        } catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface
    public String upsertGroup(String json) {
        try {
            JSONObject value = new JSONObject(json);
            AppGroup group = AppGroup.create(
                    value.getString("groupId"),
                    value.getString("displayName"),
                    strings(value.getJSONArray("packageNames")),
                    value.optBoolean("monitored", true),
                    SupervisionPolicy.of(
                            value.getLong("idleMinutes") * 60_000L,
                            minutesToMs(value.getJSONArray("checkpointsMinutes")),
                            value.getString("roleId")));
            runtime.upsertGroup(group);
            return ok().toString();
        } catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface public String removeGroup(String groupId) {
        try { runtime.removeGroup(groupId); return ok().toString(); }
        catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface public String setFeatureEnabled(boolean enabled) {
        try { runtime.setFeatureEnabled(enabled); return ok().toString(); }
        catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface public String clearRound(String groupId) {
        try { runtime.clearRound(groupId); return ok().toString(); }
        catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface public String debugSetLock(String json) {
        return directive(json, false);
    }

    @JavascriptInterface public String debugSetTemporaryUnlock(String json) {
        return directive(json, true);
    }

    @JavascriptInterface public String debugRemoveLock(String groupId) {
        try { runtime.debugRemoveLock(groupId); return ok().toString(); }
        catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface
    public String emergencyAction(String json) {
        try {
            JSONObject value = new JSONObject(json);
            String action = value.getString("action");
            String groupId = value.optString("groupId", "");
            String target = value.optString("targetGroupId", "");
            EmergencyUnlockGate.Snapshot snapshot;
            boolean resultOk = true;
            String resultError = "";
            switch (action) {
                case "begin": snapshot = runtime.emergencyBegin(groupId, target); break;
                case "hold": snapshot = runtime.emergencyHold(
                        groupId, target, value.optBoolean("holding", false)); break;
                case "reason": snapshot = runtime.emergencyReason(
                        groupId, target, value.optString("reason", "")); break;
                case "confirm":
                    EmergencyUnlockGate.Result result = runtime.emergencyConfirm(groupId, target);
                    resultOk = result.isOk();
                    resultError = result.getError();
                    snapshot = result.getSnapshot();
                    break;
                case "status": snapshot = runtime.emergencySnapshot(); break;
                default: throw new IllegalArgumentException("unknown emergency action");
            }
            JSONObject out = resultOk ? ok() : failed(resultError);
            out.put("gate", gateJson(snapshot));
            return out.toString();
        } catch (Exception exception) { return error(exception); }
    }

    @JavascriptInterface public boolean canDrawOverlays() {
        return context != null && Settings.canDrawOverlays(context);
    }

    @JavascriptInterface public void openOverlaySettings() {
        if (context == null) return;
        Intent intent = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:" + context.getPackageName()));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(intent);
    }

    private String directive(String json, boolean temporary) {
        try {
            JSONObject value = new JSONObject(json);
            if (temporary) runtime.debugSetTemporaryUnlock(
                    value.getString("groupId"), value.getInt("minutes"),
                    value.getString("roleId"), value.optString("message", ""),
                    value.getString("commandId"));
            else runtime.debugSetLock(
                    value.getString("groupId"), value.getInt("minutes"),
                    value.getString("roleId"), value.optString("message", ""),
                    value.getString("commandId"));
            return ok().toString();
        } catch (Exception exception) { return error(exception); }
    }

    private static JSONObject groupJson(AppGroup group) throws Exception {
        JSONObject out = new JSONObject();
        out.put("groupId", group.getGroupId());
        out.put("displayName", group.getDisplayName());
        out.put("packageNames", new JSONArray(group.getPackageNames()));
        out.put("monitored", group.isMonitored());
        out.put("idleMinutes", group.getPolicy().getIdleResetMs() / 60_000L);
        JSONArray checkpoints = new JSONArray();
        for (Long value : group.getPolicy().getCheckpointsMs()) checkpoints.put(value / 60_000L);
        out.put("checkpointsMinutes", checkpoints);
        out.put("roleId", group.getPolicy().getRoleId());
        return out;
    }

    private static Object directiveJson(TimedDirective directive) throws Exception {
        if (directive == null) return JSONObject.NULL;
        JSONObject out = new JSONObject();
        out.put("receivedWallMs", directive.getReceivedWallMs());
        out.put("durationMs", directive.getDurationMs());
        out.put("deadlineWallMs", directive.getDeadlineWallMs());
        out.put("deadlineElapsedMs", directive.getDeadlineElapsedMs());
        out.put("roleId", directive.getRoleId());
        out.put("message", directive.getMessage());
        out.put("commandId", directive.getCommandId());
        return out;
    }

    private static JSONObject gateJson(EmergencyUnlockGate.Snapshot snapshot) throws Exception {
        JSONObject out = new JSONObject();
        out.put("phase", snapshot.getPhase().name());
        out.put("groupId", snapshot.getGroupId());
        out.put("heldMs", snapshot.getHeldMs());
        out.put("remainingDelayMs", snapshot.getRemainingDelayMs());
        out.put("reason", snapshot.getReason());
        out.put("error", snapshot.getError());
        return out;
    }

    private static ArrayList<String> strings(JSONArray values) throws Exception {
        ArrayList<String> result = new ArrayList<>();
        for (int i = 0; i < values.length(); i++) result.add(values.getString(i));
        return result;
    }

    private static ArrayList<Long> minutesToMs(JSONArray values) throws Exception {
        ArrayList<Long> result = new ArrayList<>();
        for (int i = 0; i < values.length(); i++) {
            long minutes = values.getLong(i);
            if (minutes <= 0) throw new IllegalArgumentException("checkpoint must be positive");
            result.add(minutes * 60_000L);
        }
        return result;
    }

    private static JSONObject ok() throws Exception {
        return new JSONObject().put("ok", true);
    }

    private static JSONObject failed(String message) throws Exception {
        return new JSONObject().put("ok", false)
                .put("error", message == null ? "operation failed" : message);
    }

    private static String error(Exception exception) {
        try {
            String message = exception.getMessage() == null
                    ? exception.getClass().getSimpleName() : exception.getMessage();
            return failed(message).toString();
        } catch (Exception ignored) {
            return "{\"ok\":false,\"error\":\"bridge failure\"}";
        }
    }
}
