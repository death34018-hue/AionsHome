package com.aion.chat.miband;

public final class MiBandStatus {
    public final String state;
    public final String deviceName;
    public final boolean configured;
    public final boolean connected;
    public final boolean authenticated;
    public final boolean realtime;
    public final boolean syncing;
    public final int battery;
    public final int latestHeartRate;
    public final long latestHeartRateAt;
    public final long lastSyncAt;
    public final long nextSyncAt;
    public final String error;

    public MiBandStatus(String state, String deviceName, boolean configured,
                        boolean connected, boolean authenticated, boolean realtime,
                        boolean syncing, int battery, int latestHeartRate,
                        long latestHeartRateAt, long lastSyncAt, long nextSyncAt,
                        String error) {
        this.state = state;
        this.deviceName = deviceName;
        this.configured = configured;
        this.connected = connected;
        this.authenticated = authenticated;
        this.realtime = realtime;
        this.syncing = syncing;
        this.battery = battery;
        this.latestHeartRate = latestHeartRate;
        this.latestHeartRateAt = latestHeartRateAt;
        this.lastSyncAt = lastSyncAt;
        this.nextSyncAt = nextSyncAt;
        this.error = error;
    }
}
