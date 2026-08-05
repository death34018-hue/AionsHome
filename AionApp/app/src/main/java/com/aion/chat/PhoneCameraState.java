package com.aion.chat;

import java.util.LinkedHashSet;
import java.util.Set;

final class PhoneCameraState {
    enum Decision {
        ACCEPTED,
        DISARMED,
        EXPIRED,
        DUPLICATE,
        BUSY
    }

    private static final int RECENT_REQUEST_LIMIT = 64;

    private boolean armed;
    private String facing = "back";
    private float zoom = 1f;
    private String activeRequestId = "";
    private final Set<String> recentRequestIds = new LinkedHashSet<>();

    synchronized void arm(String requestedFacing, float requestedZoom) {
        facing = PhoneCameraImagePolicy.normalizeFacing(requestedFacing);
        zoom = Math.max(0.1f, requestedZoom);
        armed = true;
    }

    synchronized void disarm() {
        armed = false;
        activeRequestId = "";
    }

    synchronized boolean isArmed() {
        return armed;
    }

    synchronized String getFacing() {
        return facing;
    }

    synchronized float getZoom() {
        return zoom;
    }

    synchronized Decision begin(String requestId, long deadlineMs, long nowMs) {
        String normalized = requestId == null ? "" : requestId.trim();
        if (!armed) return Decision.DISARMED;
        if (normalized.isEmpty() || deadlineMs <= nowMs) return Decision.EXPIRED;
        if (normalized.equals(activeRequestId) || recentRequestIds.contains(normalized)) {
            return Decision.DUPLICATE;
        }
        if (!activeRequestId.isEmpty()) return Decision.BUSY;
        activeRequestId = normalized;
        return Decision.ACCEPTED;
    }

    synchronized void complete(String requestId) {
        String normalized = requestId == null ? "" : requestId.trim();
        if (!normalized.equals(activeRequestId)) return;
        activeRequestId = "";
        recentRequestIds.add(normalized);
        while (recentRequestIds.size() > RECENT_REQUEST_LIMIT) {
            String oldest = recentRequestIds.iterator().next();
            recentRequestIds.remove(oldest);
        }
    }
}
