package com.aion.chat;

final class WebViewLaunchPolicy {

    private WebViewLaunchPolicy() {
    }

    static String resolveUrl(
            String requestedUrl,
            boolean autoConnect,
            String savedUrl) {
        if (requestedUrl != null && !requestedUrl.trim().isEmpty()) {
            return requestedUrl;
        }
        return autoConnect ? savedUrl : null;
    }
}
