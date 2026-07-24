package com.aion.chat.supervision;

public final class TimedDirective {
    private static final long MINUTE_MS = 60_000L;

    private final long receivedElapsedMs;
    private final long receivedWallMs;
    private final long durationMs;
    private final long deadlineElapsedMs;
    private final long deadlineWallMs;
    private final String roleId;
    private final String message;
    private final String commandId;

    private TimedDirective(long receivedElapsedMs, long receivedWallMs, long durationMs,
            String roleId, String message, String commandId) {
        this.receivedElapsedMs = receivedElapsedMs;
        this.receivedWallMs = receivedWallMs;
        this.durationMs = durationMs;
        this.deadlineElapsedMs = receivedElapsedMs + durationMs;
        this.deadlineWallMs = receivedWallMs + durationMs;
        this.roleId = roleId;
        this.message = message;
        this.commandId = commandId;
    }

    public static TimedDirective create(long receivedElapsedMs, long receivedWallMs,
            int requestedMinutes, String roleId, String message, String commandId) {
        if (receivedElapsedMs < 0 || receivedWallMs < 0) {
            throw new IllegalArgumentException("received time must be nonnegative");
        }
        if (roleId == null || roleId.trim().isEmpty()) {
            throw new IllegalArgumentException("roleId is required");
        }
        if (message == null) {
            throw new IllegalArgumentException("message is required");
        }
        if (commandId == null || commandId.trim().isEmpty()) {
            throw new IllegalArgumentException("commandId is required");
        }
        int clampedMinutes = Math.max(1, Math.min(120, requestedMinutes));
        return new TimedDirective(
                receivedElapsedMs,
                receivedWallMs,
                clampedMinutes * MINUTE_MS,
                roleId.trim(),
                message,
                commandId.trim());
    }

    public boolean isActive(long nowElapsedMs) {
        return nowElapsedMs < deadlineElapsedMs;
    }

    public long getReceivedElapsedMs() { return receivedElapsedMs; }
    public long getReceivedWallMs() { return receivedWallMs; }
    public long getDurationMs() { return durationMs; }
    public long getDeadlineElapsedMs() { return deadlineElapsedMs; }
    public long getDeadlineWallMs() { return deadlineWallMs; }
    public String getRoleId() { return roleId; }
    public String getMessage() { return message; }
    public String getCommandId() { return commandId; }
}
