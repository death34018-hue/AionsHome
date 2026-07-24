package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class ConnectionEndpointTest {
    @Test
    public void migratesLegacyCloudflareHost() {
        assertEquals(ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                ConnectionEndpoint.normalizePageUrl("https://ws.aionshome.com/chat"));
        assertEquals("wss://chat.aionshome.com/ws",
                ConnectionEndpoint.toWebSocketUrl("wss://ws.aionshome.com/ws"));
    }

    @Test
    public void buildsCloudflareWebSocketOnProtectedHost() {
        assertEquals("wss://chat.aionshome.com/ws",
                ConnectionEndpoint.toWebSocketUrl("https://chat.aionshome.com/chat"));
        assertTrue(ConnectionEndpoint.isCloudflareHost("chat.aionshome.com"));
    }

    @Test
    public void preservesTailscaleAndLanRoutes() {
        assertEquals("ws://100.117.195.40:8080/ws",
                ConnectionEndpoint.toWebSocketUrl("http://100.117.195.40:8080/chat"));
        assertEquals("ws://192.168.1.92:8080/ws",
                ConnectionEndpoint.toWebSocketUrl("http://192.168.1.92:8080/chat"));
        assertFalse(ConnectionEndpoint.isCloudflareHost("100.117.195.40"));
        assertFalse(ConnectionEndpoint.isCloudflareHost("chat.aionshome.com.evil.example"));
    }

    @Test
    public void recognizesOnlyExactCloudflareAccessCookieName() {
        assertTrue(ConnectionEndpoint.hasCloudflareAccessCookie(
                "session=one; CF_Authorization=token; theme=dark"));
        assertFalse(ConnectionEndpoint.hasCloudflareAccessCookie(
                "NotCF_Authorization=token; session=one"));
        assertFalse(ConnectionEndpoint.hasCloudflareAccessCookie(null));
    }

    @Test
    public void recognizesOnlyCloudflareAccessDomainBoundaries() {
        assertTrue(ConnectionEndpoint.isCloudflareAccessHost("cloudflareaccess.com"));
        assertTrue(ConnectionEndpoint.isCloudflareAccessHost(
                "super-paper-137a.cloudflareaccess.com"));
        assertFalse(ConnectionEndpoint.isCloudflareAccessHost(
                "cloudflareaccess.com.evil.example"));
        assertFalse(ConnectionEndpoint.isCloudflareAccessHost(
                "evilcloudflareaccess.com"));
        assertFalse(ConnectionEndpoint.isCloudflareAccessHost(null));
    }

    @Test
    public void bypassesOnlyProtectedCloudflareMainDocument() {
        assertTrue(ConnectionEndpoint.shouldBypassCloudflareMainDocument(
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL, true));
        assertFalse(ConnectionEndpoint.shouldBypassCloudflareMainDocument(
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                "https://chat.aionshome.com/static/chatroom.css", false));
        assertFalse(ConnectionEndpoint.shouldBypassCloudflareMainDocument(
                "http://100.117.195.40:8080/chat",
                "http://100.117.195.40:8080/chat", true));
        assertFalse(ConnectionEndpoint.shouldBypassCloudflareMainDocument(
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                "https://chat.aionshome.com.evil.example/chat", true));
    }

    @Test
    public void allowsOnlyExactSelectedContentHost() {
        String tailscalePage = "http://100.117.195.40:8080/chat";
        assertTrue(ConnectionEndpoint.isAllowedContentHost("100.117.195.40", tailscalePage));
        assertTrue(ConnectionEndpoint.isAllowedContentHost("chat.aionshome.com",
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL));
        assertFalse(ConnectionEndpoint.isAllowedContentHost(
                "100.117.195.40.evil.example", tailscalePage));
        assertFalse(ConnectionEndpoint.isAllowedContentHost(
                "evil192.168.1.92.example", "http://192.168.1.92:8080/chat"));
    }
}
