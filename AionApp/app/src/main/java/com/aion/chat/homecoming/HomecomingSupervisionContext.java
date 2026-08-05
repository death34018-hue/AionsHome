package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;

public final class HomecomingSupervisionContext
        implements HomecomingContextBuilder.CapabilityContext {
    private static final int MAX_GROUPS = 24;
    private final HomecomingSupervisionAdapter.SnapshotSource source;

    HomecomingSupervisionContext(HomecomingSupervisionAdapter adapter) {
        this(adapter::snapshot);
    }

    HomecomingSupervisionContext(
            HomecomingSupervisionAdapter.SnapshotSource source) {
        if (source == null) throw new IllegalArgumentException("source is required");
        this.source = source;
    }

    @Override
    public String build(String mainName, String secondName) {
        try {
            HomecomingSupervisionAdapter.Snapshot snapshot = source.snapshot();
            if (!snapshot.enabled || "unavailable".equals(snapshot.readiness)) {
                return "";
            }
            StringBuilder output = new StringBuilder();
            output.append("应用监督由手机本地执行，当前已启用。");
            if ("degraded".equals(snapshot.readiness)) {
                output.append("前台识别权限处于降级状态；已有锁定仍由手机本地执行，"
                        + "不得声称已停止或已解除。");
            }
            output.append("\n可使用且只可使用以下既有指令，每次回复最多一条：")
                    .append("\n[APP_LOCK:groupId|分钟|提示]")
                    .append("\n[APP_TEMP_UNLOCK:groupId|分钟|说明]")
                    .append("\n[APP_UNLOCK:groupId]");
            JSONArray groups = snapshot.state.optJSONArray("groups");
            if (groups == null || groups.length() == 0) {
                output.append("\n当前没有配置可监督的应用组。");
                return output.toString();
            }
            output.append("\n当前手机监督状态：");
            int limit = Math.min(groups.length(), MAX_GROUPS);
            for (int index = 0; index < limit; index++) {
                JSONObject group = groups.optJSONObject(index);
                if (group == null) continue;
                String groupId = group.optString("groupId", "").trim();
                String displayName = group.optString("displayName", "").trim();
                if (groupId.isEmpty() || displayName.isEmpty()) continue;
                String roleLabel = configuredRoleLabel(
                        group.optString("roleId", ""), mainName, secondName);
                long usageMinutes = Math.max(
                        0L, group.optLong("roundUsageMs", 0L) / 60_000L);
                String effectiveState =
                        group.optString("effectiveState", "NORMAL").trim();
                output.append("\n- groupId=")
                        .append(groupId)
                        .append("；名称=")
                        .append(displayName)
                        .append("；负责角色=")
                        .append(roleLabel)
                        .append("；本轮使用=")
                        .append(usageMinutes)
                        .append("分钟；状态=")
                        .append(effectiveState.isEmpty() ? "NORMAL" : effectiveState);
            }
            return output.toString();
        } catch (Exception exception) {
            return "";
        }
    }

    private static String configuredRoleLabel(
            String roleId, String mainName, String secondName) {
        String stable = roleId == null ? "" : roleId.trim().toLowerCase();
        if ("second".equals(stable) || "connor".equals(stable)) {
            return configured(secondName);
        }
        if ("main".equals(stable) || "aion".equals(stable)) {
            return configured(mainName);
        }
        return "未配置角色";
    }

    private static String configured(String value) {
        return value == null || value.trim().isEmpty() ? "AI" : value.trim();
    }
}
