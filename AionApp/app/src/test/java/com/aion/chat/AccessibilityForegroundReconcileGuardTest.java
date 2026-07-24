package com.aion.chat;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class AccessibilityForegroundReconcileGuardTest {
    @Test
    public void accessibilityCallbackDebouncesWithoutTraversingWindowRoots() throws Exception {
        String source = readSource(
                "app/src/main/java/com/aion/chat/AionAccessibilityService.java");

        assertFalse(source.contains("getWindows("));
        assertFalse(source.contains(".getRoot("));
        assertFalse(source.contains("ForegroundWindowResolver"));
        assertTrue(source.contains("FOREGROUND_RECONCILE_DELAY_MS = 400L"));
        assertTrue(source.contains(
                "mainHandler.postDelayed(foregroundReconcile, "
                        + "FOREGROUND_RECONCILE_DELAY_MS)"));
        assertTrue(source.contains("runtime.reconcileForegroundOnce()"));
    }

    @Test
    public void usageStatsQueriesRunOnNamedBackgroundExecutor() throws Exception {
        String source = readSource(
                "app/src/main/java/com/aion/chat/supervision/ForegroundAppDetector.java");

        assertTrue(source.contains("Executors.newSingleThreadScheduledExecutor"));
        assertTrue(source.contains("\"AionForegroundDetector\""));
        assertTrue(source.contains("worker.schedule"));
        assertTrue(source.contains("mainHandler.post"));
        assertTrue(source.contains("LatestRequestGate"));
    }

    private static String readSource(String relativePath) throws Exception {
        Path path = Paths.get(relativePath);
        if (!Files.exists(path)) {
            path = Paths.get("..").resolve(relativePath);
        }
        return new String(Files.readAllBytes(path), StandardCharsets.UTF_8);
    }
}
