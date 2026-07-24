package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class CloudflareReauthStateTest {
    @Test
    public void bypassesExactlyOneProtectedMainDocumentPerAuthenticationAttempt() {
        CloudflareReauthState state = new CloudflareReauthState();

        assertFalse(state.begin("http://100.117.195.40:8080/chat"));
        assertTrue(state.begin(ConnectionEndpoint.CLOUDFLARE_PAGE_URL));
        assertFalse(state.begin(ConnectionEndpoint.CLOUDFLARE_PAGE_URL));

        assertFalse(state.shouldBypass(
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                "https://chat.aionshome.com/static/chatroom.css", false));
        assertTrue(state.shouldBypass(
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL, true));
        assertFalse(state.shouldBypass(
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL,
                ConnectionEndpoint.CLOUDFLARE_PAGE_URL, true));

        state.authenticationCompleted();
        assertTrue(state.begin(ConnectionEndpoint.CLOUDFLARE_PAGE_URL));
    }
}
