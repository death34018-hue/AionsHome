package com.aion.chat.supervision;

public final class EngineEvent {
    public enum Type {
        CHECKPOINT_REACHED,
        ROUND_RESET,
        USAGE_INTERVAL_CLOSED
    }

    private final Type type;
    private final String groupId;
    private final long elapsedMs;
    private final long wallMs;
    private final long checkpointMs;

    private EngineEvent(Type type, String groupId, long elapsedMs, long wallMs,
            long checkpointMs) {
        this.type = type;
        this.groupId = groupId;
        this.elapsedMs = elapsedMs;
        this.wallMs = wallMs;
        this.checkpointMs = checkpointMs;
    }

    static EngineEvent intervalClosed(String groupId, long elapsedMs, long wallMs) {
        return new EngineEvent(Type.USAGE_INTERVAL_CLOSED, groupId, elapsedMs, wallMs, 0L);
    }

    static EngineEvent checkpoint(String groupId, long elapsedMs, long wallMs,
            long checkpointMs) {
        return new EngineEvent(
                Type.CHECKPOINT_REACHED, groupId, elapsedMs, wallMs, checkpointMs);
    }

    static EngineEvent roundReset(String groupId, long elapsedMs, long wallMs) {
        return new EngineEvent(Type.ROUND_RESET, groupId, elapsedMs, wallMs, 0L);
    }

    public Type getType() { return type; }
    public String getGroupId() { return groupId; }
    public long getElapsedMs() { return elapsedMs; }
    public long getWallMs() { return wallMs; }
    public long getCheckpointMs() { return checkpointMs; }
}
