package com.aion.chat;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class SupervisionIntegrationGuardTest {
    private static String methodBody(String source, String signature) {
        int start = source.indexOf(signature);
        if (start < 0) return "";
        int open = source.indexOf('{', start);
        int depth = 0;
        for (int index = open; index < source.length(); index++) {
            char value = source.charAt(index);
            if (value == '{') depth++;
            if (value == '}' && --depth == 0) {
                return source.substring(open + 1, index);
            }
        }
        return "";
    }

    @Test
    public void legacyActivityLoopIsSilentOffScreenAndCannotBypassRecoveryController()
            throws Exception {
        String source = new String(Files.readAllBytes(Paths.get(
                "src/main/java/com/aion/chat/AionPushService.java")),
                StandardCharsets.UTF_8);

        assertTrue(source.contains("if (screenOn && hasUsageStatsPermission())"));
        assertFalse(source.contains("                checkAndRecoverAccessibility();"));
    }

    @Test
    public void appSupervisionCommandsEnterOneMainThreadExecutionBoundary()
            throws Exception {
        String source = new String(Files.readAllBytes(Paths.get(
                "src/main/java/com/aion/chat/AionPushService.java")),
                StandardCharsets.UTF_8);

        String dispatcher = methodBody(
                source, "private void dispatchAppSupervisionCommand(JSONObject data)");
        String executor = methodBody(
                source, "private void applyAppSupervisionCommandOnMain(JSONObject data)");

        assertTrue(source.contains("dispatchAppSupervisionCommand(command);"));
        assertTrue(source.contains("dispatchAppSupervisionCommand(data);"));
        assertTrue(dispatcher.contains(
                "mainHandler.post(() -> applyAppSupervisionCommandOnMain(data))"));
        assertTrue(executor.contains("runtime.applyAiCommand("));
        int applyIndex = executor.indexOf("runtime.applyAiCommand(");
        int storeIndex = executor.indexOf("storeAppSupervisionResult(", applyIndex);
        int freshAckIndex = executor.indexOf("ackAppSupervisionCommand(", storeIndex);
        assertTrue(applyIndex < storeIndex);
        assertTrue(storeIndex < freshAckIndex);
        assertFalse(source.contains("offerAppSupervisionCommand("));
    }

    @Test
    public void wholeDeviceOverlayUsesDedicatedHomeButtonCopy() throws Exception {
        String strings = new String(Files.readAllBytes(Paths.get(
                "src/main/res/values/strings.xml")), StandardCharsets.UTF_8);
        String overlay = new String(Files.readAllBytes(Paths.get(
                "src/main/java/com/aion/chat/supervision/AppSupervisionOverlay.java")),
                StandardCharsets.UTF_8);

        assertTrue(strings.contains(
                "<string name=\"device_lock_home_button\">乖乖回小家呆着</string>"));
        assertTrue(overlay.contains("deviceLock ? context.getString("));
        assertTrue(overlay.contains(
                "com.aion.chat.R.string.device_lock_home_button) : \"返回桌面\""));
    }
}
