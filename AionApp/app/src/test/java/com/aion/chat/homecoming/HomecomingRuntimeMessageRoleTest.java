package com.aion.chat.homecoming;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingRuntimeMessageRoleTest {
    @Test
    public void systemResultsRenderWithAssistantSideInsteadOfUserSide() {
        assertFalse(HomecomingRuntime.isAssistantProjection("user"));
        assertTrue(HomecomingRuntime.isAssistantProjection("assistant"));
        assertTrue(HomecomingRuntime.isAssistantProjection("system"));
    }
}
