package com.aion.chat;

import java.net.URI;

final class TtsAudioUrlResolver {
    private TtsAudioUrlResolver() {
    }

    static String resolve(String pageUrl, String playbackUrl) {
        if (pageUrl == null || playbackUrl == null || playbackUrl.trim().isEmpty()) {
            return null;
        }
        try {
            URI resolved = URI.create(pageUrl).resolve(playbackUrl.trim());
            String scheme = resolved.getScheme();
            if (!"http".equalsIgnoreCase(scheme) && !"https".equalsIgnoreCase(scheme)) {
                return null;
            }
            return resolved.toString();
        } catch (IllegalArgumentException ignored) {
            return null;
        }
    }
}
