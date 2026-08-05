package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.lang.reflect.Method;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingChatAssetContractTest {
    @Test
    public void bundledShellContainsThreeChatsMediaTtsAndMemoryControls() throws Exception {
        String html = read("src/main/assets/homecoming/index.html");
        String js = read("src/main/assets/homecoming/homecoming.js");
        assertTrue(html.contains("data-timeline=\"main_private\""));
        assertTrue(html.contains("data-timeline=\"companion_private\""));
        assertTrue(html.contains("data-timeline=\"group\""));
        assertTrue(html.contains("id=\"composer\""));
        assertTrue(html.contains("id=\"pickImage\""));
        assertTrue(html.contains("id=\"captureImage\""));
        assertTrue(html.contains("id=\"ttsEnabled\""));
        assertTrue(html.contains("id=\"memoryPanel\""));
        assertTrue(html.contains("id=\"summarizeMemories\""));
        assertTrue(html.contains("id=\"scheduleTab\""));
        assertTrue(html.contains("id=\"schedulePanel\""));
        assertTrue(html.contains("id=\"scheduleList\""));
        assertTrue(html.contains("id=\"scheduleType\""));
        assertTrue(html.contains("id=\"scheduleTime\""));
        assertTrue(html.contains("id=\"scheduleContent\""));
        assertTrue(html.contains("id=\"scheduleOwner\""));
        assertTrue(html.contains("id=\"scheduleTimeline\""));
        assertTrue(js.contains("HomecomingNative.sendMessage"));
        assertTrue(js.contains("HomecomingNative.createMemory"));
        assertTrue(js.contains("HomecomingNative.updateMemory"));
        assertTrue(js.contains("HomecomingNative.deleteMemory"));
        assertTrue(js.contains("HomecomingNative.summarizeAllMemories"));
        assertTrue(js.contains("HomecomingNative.listSchedules"));
        assertTrue(js.contains("HomecomingNative.createSchedule"));
        assertTrue(js.contains("HomecomingNative.deleteSchedule"));
        assertTrue(js.contains("scheduleExactness"));
        assertTrue(js.contains("\"schedule_changed\""));
        assertTrue(js.contains("\"schedule_fired\""));
        assertTrue(js.contains("\"schedule_failed\""));
    }

    @Test
    public void chatRenderingNeverTreatsMessagesAsMarkup() throws Exception {
        String js = read("src/main/assets/homecoming/homecoming.js");
        assertTrue(js.contains(".textContent"));
        assertFalse(js.contains("message.innerHTML"));
        assertFalse(js.contains("insertAdjacentHTML"));
        assertFalse(js.contains("fetch("));
        assertFalse(js.contains("XMLHttpRequest"));
    }

    @Test
    public void supervisionPanelIsReadOnlyAndUsesTypedLocalBridge() throws Exception {
        String html = read("src/main/assets/homecoming/index.html");
        String js = read("src/main/assets/homecoming/homecoming.js");
        Method status = HomecomingBridge.class.getDeclaredMethod(
                "getSupervisionStatusJson");

        assertTrue(html.contains("id=\"supervisionTab\""));
        assertTrue(html.contains("id=\"supervisionPanel\""));
        assertTrue(html.contains("id=\"supervisionList\""));
        assertTrue(js.contains("HomecomingNative.getSupervisionStatusJson"));
        assertTrue(js.contains("renderSupervisionStatus"));
        assertTrue(js.contains(".textContent"));
        assertFalse(js.contains("HomecomingNative.setSupervision"));
        assertFalse(js.contains("HomecomingNative.updateSupervision"));
        assertFalse(js.contains("HomecomingNative.deleteSupervision"));
        assertTrue(status.getParameterTypes().length == 0);
        assertTrue(status.getReturnType() == String.class);
    }

    @Test
    public void activeShellUsesCompactConversationPickerAndFourPageNavigation()
            throws Exception {
        String html = read("src/main/assets/homecoming/index.html");
        String js = read("src/main/assets/homecoming/homecoming.js");

        assertTrue(html.contains("id=\"conversationPicker\""));
        assertTrue(html.contains("data-page=\"chat\""));
        assertTrue(html.contains("data-page=\"memory\""));
        assertTrue(html.contains("data-page=\"schedule\""));
        assertTrue(html.contains("data-page=\"settings\""));
        assertTrue(html.contains("id=\"scheduleTab\""));
        assertTrue(html.contains("id=\"supervisionTab\""));
        assertFalse(html.contains("class=\"timeline-tabs\""));
        assertFalse(html.contains("id=\"responderSelect\""));
        assertTrue(js.contains("switchPage("));
        assertTrue(js.contains("switchTimeline("));
    }

    @Test
    public void memorySummaryIsManualAndRunsBothConfiguredOwners() throws Exception {
        String js = read("src/main/assets/homecoming/homecoming.js");
        String runtime = read(
                "src/main/java/com/aion/chat/homecoming/HomecomingRuntime.java");
        Method summarize = HomecomingBridge.class.getDeclaredMethod(
                "summarizeAllMemories");

        assertTrue(js.contains("HomecomingNative.summarizeAllMemories()"));
        assertFalse(js.contains("HomecomingNative.summarizeMemories("));
        assertFalse(runtime.contains("summarizeMemories(\n"
                + "                        responderOwner"));
        assertTrue(summarize.getParameterTypes().length == 0);
        assertTrue(summarize.getReturnType() == void.class);
    }

    private static String read(String path) throws Exception {
        return new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8);
    }
}
