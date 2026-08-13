package com.aion.chat;

import android.webkit.CookieManager;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import okhttp3.Cookie;
import okhttp3.CookieJar;
import okhttp3.HttpUrl;

/** Shares WebView authentication and device cookies with background OkHttp calls. */
public final class WebViewCookieJar implements CookieJar {
    interface Store {
        String get(String url);
        void set(String url, String cookie);
        void flush();
    }

    private final Store store;

    public WebViewCookieJar() {
        this(new AndroidStore());
    }

    WebViewCookieJar(Store store) {
        this.store = store;
    }

    @Override
    public void saveFromResponse(HttpUrl url, List<Cookie> cookies) {
        for (Cookie cookie : cookies) {
            store.set(url.toString(), cookie.toString());
        }
        if (!cookies.isEmpty()) store.flush();
    }

    @Override
    public List<Cookie> loadForRequest(HttpUrl url) {
        String raw = store.get(url.toString());
        if (raw == null || raw.trim().isEmpty()) return Collections.emptyList();
        List<Cookie> cookies = new ArrayList<>();
        for (String part : raw.split(";")) {
            String value = part.trim();
            int equals = value.indexOf('=');
            if (equals <= 0) continue;
            try {
                cookies.add(new Cookie.Builder()
                        .name(value.substring(0, equals).trim())
                        .value(value.substring(equals + 1).trim())
                        .hostOnlyDomain(url.host())
                        .path("/")
                        .build());
            } catch (IllegalArgumentException ignored) {
                // Ignore malformed browser cookies without breaking networking.
            }
        }
        return cookies;
    }

    private static final class AndroidStore implements Store {
        private final CookieManager manager = CookieManager.getInstance();

        @Override public String get(String url) {
            return manager.getCookie(url);
        }

        @Override public void set(String url, String cookie) {
            manager.setCookie(url, cookie);
        }

        @Override public void flush() {
            manager.flush();
        }
    }
}
