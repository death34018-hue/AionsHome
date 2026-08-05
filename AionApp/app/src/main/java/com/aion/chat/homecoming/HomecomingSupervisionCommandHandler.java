package com.aion.chat.homecoming;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

public final class HomecomingSupervisionCommandHandler {
    private static final long COMMAND_TTL_MS = 5 * 60_000L;
    private static final int MAX_MESSAGE_LENGTH = 500;

    private final RuntimePort runtime;
    private final String epochId;

    HomecomingSupervisionCommandHandler(RuntimePort runtime, String epochId) {
        if (runtime == null) throw new IllegalArgumentException("runtime is required");
        if (epochId == null || epochId.trim().isEmpty()) {
            throw new IllegalArgumentException("epochId is required");
        }
        this.runtime = runtime;
        this.epochId = epochId.trim();
    }

    public ApplyResult apply(
            String requestId,
            int index,
            HomecomingControlParser.ControlEvent event,
            String ownerId,
            long now) {
        if (event == null || !"app_supervision".equals(event.type)) {
            return ApplyResult.deferred();
        }
        if (requestId == null || requestId.trim().isEmpty() || index < 0) {
            return ApplyResult.invalid();
        }
        Parsed parsed = parse(event);
        if (parsed == null) return ApplyResult.invalid();
        String roleId = stableRoleId(ownerId);
        if (roleId.isEmpty()) return ApplyResult.invalid();
        String commandId = UUID.nameUUIDFromBytes(
                (epochId + ":" + requestId.trim() + ":" + index + ":" + parsed.action)
                        .getBytes(StandardCharsets.UTF_8)).toString();
        RuntimeResult result;
        try {
            result = runtime.apply(
                    parsed.action,
                    parsed.groupId,
                    parsed.minutes,
                    roleId,
                    parsed.message,
                    commandId,
                    now + COMMAND_TTL_MS);
        } catch (RuntimeException exception) {
            result = new RuntimeResult(false, "execution_failed", "");
        }
        String status = result.success
                ? "applied"
                : normalizeReason(result.reason);
        return new ApplyResult(
                status,
                commandId,
                parsed.action,
                parsed.minutes,
                result.groupDisplayName,
                result.success);
    }

    private static Parsed parse(HomecomingControlParser.ControlEvent event) {
        String upper = event.rawTag.toUpperCase(Locale.ROOT);
        List<String> values = event.arguments;
        if (upper.startsWith("[APP_LOCK:")) {
            return timed("lock", values);
        }
        if (upper.startsWith("[APP_TEMP_UNLOCK:")) {
            return timed("temp_unlock", values);
        }
        if (upper.startsWith("[APP_UNLOCK:")) {
            if (values.size() != 1) return null;
            return new Parsed("unlock", values.get(0), 0, "");
        }
        return null;
    }

    private static Parsed timed(String action, List<String> values) {
        if (values.size() != 3 || values.get(2).length() > MAX_MESSAGE_LENGTH) {
            return null;
        }
        try {
            int minutes = Integer.parseInt(values.get(1));
            if (minutes <= 0) return null;
            return new Parsed(action, values.get(0), minutes, values.get(2));
        } catch (NumberFormatException exception) {
            return null;
        }
    }

    private static String stableRoleId(String ownerId) {
        if ("main".equals(ownerId)) return "aion";
        if ("second".equals(ownerId)) return "connor";
        return "";
    }

    private static String normalizeReason(String value) {
        if ("feature_disabled".equals(value)
                || "unknown_group".equals(value)
                || "expired".equals(value)
                || "missing_command_id".equals(value)
                || "unknown_action".equals(value)) {
            return value;
        }
        return "execution_failed";
    }

    interface RuntimePort {
        RuntimeResult apply(
                String action,
                String groupId,
                int minutes,
                String roleId,
                String message,
                String commandId,
                long expiresWallMs);
    }

    public static final class RuntimeResult {
        public final boolean success;
        public final String reason;
        public final String groupDisplayName;

        RuntimeResult(boolean success, String reason, String groupDisplayName) {
            this.success = success;
            this.reason = reason == null ? "" : reason;
            this.groupDisplayName =
                    groupDisplayName == null ? "" : groupDisplayName.trim();
        }
    }

    public static final class ApplyResult {
        public final String status;
        public final String commandId;
        public final String action;
        public final int minutes;
        public final String groupDisplayName;
        public final boolean applied;

        ApplyResult(
                String status,
                String commandId,
                String action,
                int minutes,
                String groupDisplayName,
                boolean applied) {
            this.status = status;
            this.commandId = commandId;
            this.action = action;
            this.minutes = minutes;
            this.groupDisplayName =
                    groupDisplayName == null ? "" : groupDisplayName;
            this.applied = applied;
        }

        static ApplyResult deferred() {
            return new ApplyResult("deferred", "", "", 0, "", false);
        }

        static ApplyResult invalid() {
            return new ApplyResult("invalid", "", "", 0, "", false);
        }
    }

    private static final class Parsed {
        final String action;
        final String groupId;
        final int minutes;
        final String message;

        Parsed(String action, String groupId, int minutes, String message) {
            this.action = action;
            this.groupId = groupId;
            this.minutes = minutes;
            this.message = message;
        }
    }
}
