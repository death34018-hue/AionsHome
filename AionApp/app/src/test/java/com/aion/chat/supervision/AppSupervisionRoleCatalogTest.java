package com.aion.chat.supervision;

import org.junit.Test;

import java.util.Map;

import static org.junit.Assert.assertEquals;

public class AppSupervisionRoleCatalogTest {
    @Test
    public void parsesConfiguredDisplayNamesWithoutHardcodedRoleNames() throws Exception {
        Map<String, String> labels = AppSupervisionRoleCatalog.fromRuntimeConfig(
                "{\"roles\":["
                        + "{\"id\":\"connor\",\"label\":\"Configured Companion\"},"
                        + "{\"id\":\"aion\",\"label\":\"Configured Main AI\"},"
                        + "{\"id\":\"empty\",\"label\":\"\"}]}"
        );

        assertEquals("Configured Companion", labels.get("connor"));
        assertEquals("Configured Main AI", labels.get("aion"));
        assertEquals(2, labels.size());
    }
}
