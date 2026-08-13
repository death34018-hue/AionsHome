package com.aion.chat;

import java.net.URI;
import java.net.URISyntaxException;

/** Keeps public and private connection routes isolated. */
final class ConnectionEndpoint {
    static final String CLOUDFLARE_HOST = "chat.example.com";
    static final String CLOUDFLARE_ACCESS_HOST = "cloudflareaccess.com";
    static final String LEGACY_CLOUDFLARE_WS_HOST = "legacy-ws.example.com";
    static final String CLOUDFLARE_PAGE_URL = "https://" + CLOUDFLARE_HOST + "/chat";
    static final String CLOUDFLARE_COOKIE_URL = "https://" + CLOUDFLARE_HOST + "/";

    private ConnectionEndpoint() {}

    static String normalizePageUrl(String rawUrl) {
        if (rawUrl == null) return null;
        String value = rawUrl.trim();
        try {
            URI uri = new URI(value);
            if (LEGACY_CLOUDFLARE_WS_HOST.equalsIgnoreCase(uri.getHost())) {
                return CLOUDFLARE_PAGE_URL;
            }
        } catch (URISyntaxException ignored) {
            // Preserve invalid input so the existing UI can report it.
        }
        return value;
    }

    static String toWebSocketUrl(String rawUrl) {
        String value = normalizePageUrl(rawUrl);
        if (value == null) return null;
        try {
            URI uri = new URI(value);
            String scheme = uri.getScheme();
            if (scheme == null) return value;
            String wsScheme;
            if ("https".equalsIgnoreCase(scheme) || "wss".equalsIgnoreCase(scheme)) {
                wsScheme = "wss";
            } else if ("http".equalsIgnoreCase(scheme) || "ws".equalsIgnoreCase(scheme)) {
                wsScheme = "ws";
            } else {
                return value;
            }
            return new URI(wsScheme, uri.getUserInfo(), uri.getHost(), uri.getPort(),
                    "/ws", null, null).toString();
        } catch (URISyntaxException ignored) {
            String ws = value.replace("http://", "ws://").replace("https://", "wss://");
            ws = ws.replace("/chat", "/ws");
            return ws.endsWith("/ws") ? ws : ws + "/ws";
        }
    }

    static boolean isCloudflareHost(String host) {
        return host != null && CLOUDFLARE_HOST.equalsIgnoreCase(host);
    }

    static boolean isCloudflareAccessHost(String host) {
        if (host == null) return false;
        String normalized = host.toLowerCase(java.util.Locale.ROOT);
        return CLOUDFLARE_ACCESS_HOST.equals(normalized)
                || normalized.endsWith("." + CLOUDFLARE_ACCESS_HOST);
    }

    static boolean isCloudflareUrl(String rawUrl) {
        if (rawUrl == null) return false;
        try {
            return isCloudflareHost(new URI(rawUrl).getHost());
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static boolean isCloudflareAccessUrl(String rawUrl) {
        if (rawUrl == null) return false;
        try {
            return isCloudflareAccessHost(new URI(rawUrl).getHost());
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    static boolean shouldBypassCloudflareMainDocument(
            String targetUrl, String requestUrl, boolean mainFrame) {
        if (!mainFrame || !isCloudflareUrl(targetUrl) || !isCloudflareUrl(requestUrl)) {
            return false;
        }
        try {
            URI target = new URI(targetUrl);
            URI request = new URI(requestUrl);
            return canonicalPath(target.getPath()).equals(canonicalPath(request.getPath()));
        } catch (URISyntaxException ignored) {
            return false;
        }
    }

    private static String canonicalPath(String path) {
        if (path == null || path.isEmpty()) return "/";
        if (path.length() > 1 && path.endsWith("/")) {
            return path.substring(0, path.length() - 1);
        }
        return path;
    }

    static boolean hasCloudflareAccessCookie(String cookieHeader) {
        if (cookieHeader == null || cookieHeader.trim().isEmpty()) return false;
        for (String part : cookieHeader.split(";")) {
            int equals = part.indexOf('=');
            String name = equals >= 0 ? part.substring(0, equals).trim() : part.trim();
            if ("CF_Authorization".equals(name)) return true;
        }
        return false;
    }

    static boolean isAllowedContentHost(String host, String currentPageUrl) {
        if (host == null) return false;
        if (isCloudflareHost(host)
                || "localhost".equalsIgnoreCase(host)
                || "127.0.0.1".equals(host)) {
            return true;
        }
        try {
            String targetHost = new URI(currentPageUrl).getHost();
            return targetHost != null && targetHost.equalsIgnoreCase(host);
        } catch (Exception ignored) {
            return false;
        }
    }
}
