package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import static org.junit.Assert.assertTrue;

public class HomecomingEpochMessageIsolationContractTest {
    @Test
    public void runtimeTimelineReadsOnlyLocalMessagesFromTheCurrentEpoch()
            throws Exception {
        String source = new String(
                Files.readAllBytes(Paths.get(
                        "src/main/java/com/aion/chat/homecoming/HomecomingRuntime.java")),
                StandardCharsets.UTF_8);

        assertTrue(source.contains(
                "WHERE timeline_id=? AND epoch_id=? AND created_at<?"));
        assertTrue(source.contains(
                "new String[]{timelineId, epochId, String.valueOf(before)}"));
    }
}
