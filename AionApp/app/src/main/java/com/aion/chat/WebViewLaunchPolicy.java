package com.aion.chat;

import java.net.URI;
import java.net.URLEncoder;
import java.io.UnsupportedEncodingException;
import java.net.URISyntaxException;

final class WebViewLaunchPolicy {

    private WebViewLaunchPolicy() {
    }

    static String resolveUrl(
            String requestedUrl,
            boolean autoConnect,
            String savedUrl) {
        if (requestedUrl != null && !requestedUrl.trim().isEmpty()) {
            return toShellUrl(requestedUrl);
        }
        return autoConnect ? toShellUrl(savedUrl) : null;
    }

    static boolean canReuseChatShell(String currentUrl, String requestedUrl) {
        if (currentUrl == null || requestedUrl == null) return false;
        try {
            URI page = new URI(currentUrl);
            URI target = new URI(requestedUrl);
            return ("http".equalsIgnoreCase(page.getScheme()) || "https".equalsIgnoreCase(page.getScheme()))
                    && page.getHost() != null && "/chat".equals(page.getRawPath())
                    && page.resolve("/").equals(target.resolve("/"));
        } catch (URISyntaxException invalidUrl) {
            return false;
        }
    }

    /** Every native entry keeps navigation and system-bar insets in /chat. */
    static String toShellUrl(String requestedUrl) {
        String url = ConnectionEndpoint.normalizePageUrl(requestedUrl);
        if (url == null || url.isEmpty()) return url;
        try {
            URI uri = new URI(url);
            if (!("http".equalsIgnoreCase(uri.getScheme())
                    || "https".equalsIgnoreCase(uri.getScheme())) || uri.getHost() == null) {
                return url;
            }
            String path = uri.getRawPath();
            if ("/chat".equals(path)) return url;
            String destination = (path == null || path.isEmpty()) ? "/" : path;
            if (uri.getRawQuery() != null) destination += "?" + uri.getRawQuery();
            if (uri.getRawFragment() != null) destination += "#" + uri.getRawFragment();
            String shell = uri.getScheme() + "://" + uri.getRawAuthority() + "/chat";
            return "/".equals(destination) ? shell
                    : shell + "?page=" + URLEncoder.encode(destination, "UTF-8").replace("+", "%20");
        } catch (URISyntaxException invalidUrl) {
            return url;
        } catch (UnsupportedEncodingException impossible) {
            throw new AssertionError(impossible);
        }
    }
}
