package com.aion.chat.widget;

public final class PrivateMemo {
    public final long id;
    public final String serverId;
    public final String content;
    public final String status;
    public final String source;
    public final long createdAt;
    public final long updatedAt;
    public final String syncState;

    PrivateMemo(long id, String serverId, String content, String status, String source,
                long createdAt, long updatedAt, String syncState) {
        this.id = id;
        this.serverId = serverId;
        this.content = content;
        this.status = status;
        this.source = source;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
        this.syncState = syncState;
    }
}
