package com.aion.chat.supervision;

public final class SupervisionTime {
    private final long elapsedMs;
    private final long wallMs;

    public SupervisionTime(long elapsedMs, long wallMs) {
        if (elapsedMs < 0 || wallMs < 0) {
            throw new IllegalArgumentException("time must be nonnegative");
        }
        this.elapsedMs = elapsedMs;
        this.wallMs = wallMs;
    }

    public long getElapsedMs() {
        return elapsedMs;
    }

    public long getWallMs() {
        return wallMs;
    }
}
