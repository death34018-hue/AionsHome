package com.aion.chat;

import java.util.concurrent.atomic.AtomicBoolean;

/** Coordinates one real Cloudflare navigation while keeping persistent assets cached. */
final class CloudflareReauthState {
    private final AtomicBoolean inProgress = new AtomicBoolean(false);
    private final AtomicBoolean bypassMainDocumentOnce = new AtomicBoolean(false);

    boolean begin(String targetUrl) {
        if (!ConnectionEndpoint.isCloudflareUrl(targetUrl)
                || !inProgress.compareAndSet(false, true)) {
            return false;
        }
        bypassMainDocumentOnce.set(true);
        return true;
    }

    boolean shouldBypass(String targetUrl, String requestUrl, boolean mainFrame) {
        return bypassMainDocumentOnce.get()
                && ConnectionEndpoint.shouldBypassCloudflareMainDocument(
                        targetUrl, requestUrl, mainFrame)
                && bypassMainDocumentOnce.compareAndSet(true, false);
    }

    void authenticationCompleted() {
        bypassMainDocumentOnce.set(false);
        inProgress.set(false);
    }
}
