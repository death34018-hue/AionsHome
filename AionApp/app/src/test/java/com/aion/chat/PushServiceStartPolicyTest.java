package com.aion.chat;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class PushServiceStartPolicyTest {
    @Test
    public void foregroundStatusActionCannotShortCircuitColdStart() {
        assertFalse(PushServiceStartPolicy.canReturnAfterLightweightAction(
                "set_foreground", null, false));
        assertFalse(PushServiceStartPolicy.canReturnAfterLightweightAction(
                "set_foreground", "ws://100.64.0.1:8080/ws", false));

        assertTrue(PushServiceStartPolicy.canReturnAfterLightweightAction(
                "set_foreground", "ws://100.64.0.1:8080/ws", true));
    }

    @Test
    public void cloudflareAuthActionCannotShortCircuitColdStartWithoutEndpoint() {
        assertFalse(PushServiceStartPolicy.canReturnAfterLightweightAction(
                AionPushService.ACTION_REFRESH_CLOUDFLARE_AUTH, null, false));

        assertTrue(PushServiceStartPolicy.canReturnAfterLightweightAction(
                AionPushService.ACTION_REFRESH_CLOUDFLARE_AUTH,
                "wss://chat.example.com/ws",
                true));
    }

    @Test
    public void fallbackUrlPrefersLastActiveRouteBeforeRememberedRoute() {
        assertEquals("http://100.64.0.1:8080/chat",
                PushServiceStartPolicy.chooseFallbackPageUrl(
                        "http://100.64.0.1:8080/chat",
                        "http://192.168.1.100:8080/chat",
                        "http://127.0.0.1:8080/chat"));

        assertEquals("http://192.168.1.100:8080/chat",
                PushServiceStartPolicy.chooseFallbackPageUrl(
                        "",
                        "http://192.168.1.100:8080/chat",
                        "http://127.0.0.1:8080/chat"));
    }
}
