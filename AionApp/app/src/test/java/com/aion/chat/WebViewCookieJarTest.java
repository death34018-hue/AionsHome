package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import okhttp3.Cookie;
import okhttp3.HttpUrl;
import org.junit.Test;

public class WebViewCookieJarTest {
    @Test
    public void responseDeviceCookieIsAvailableToLaterBackgroundRequests() {
        FakeStore store = new FakeStore();
        WebViewCookieJar jar = new WebViewCookieJar(store);
        HttpUrl url = HttpUrl.get("http://192.168.1.178:8080/api/app-supervision/state");
        Cookie deviceCookie = Cookie.parse(
                url,
                "aion_device=stable.signed; Path=/; HttpOnly; Max-Age=63072000");

        jar.saveFromResponse(url, java.util.Collections.singletonList(deviceCookie));
        List<Cookie> loaded = jar.loadForRequest(url);

        assertTrue(store.cookies.get(url.toString()).get(0).startsWith("aion_device=stable.signed"));
        assertEquals(1, loaded.size());
        assertEquals("aion_device", loaded.get(0).name());
        assertEquals("stable.signed", loaded.get(0).value());
        assertEquals(1, store.flushCount);
    }

    private static final class FakeStore implements WebViewCookieJar.Store {
        final Map<String, List<String>> cookies = new HashMap<>();
        int flushCount;

        @Override public String get(String url) {
            List<String> values = cookies.get(url);
            return values == null ? null : String.join("; ", values);
        }

        @Override public void set(String url, String cookie) {
            cookies.computeIfAbsent(url, ignored -> new ArrayList<>())
                    .add(cookie.split(";", 2)[0]);
        }

        @Override public void flush() {
            flushCount++;
        }
    }
}
