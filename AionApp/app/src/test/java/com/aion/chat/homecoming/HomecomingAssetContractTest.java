package com.aion.chat.homecoming;

import android.webkit.JavascriptInterface;

import org.junit.Test;

import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingAssetContractTest {
    @Test
    public void assetsUseOnlyBundledRelativeResources() throws Exception {
        String html = read("src/main/assets/homecoming/index.html");
        assertTrue(html.contains("href=\"homecoming.css\""));
        assertTrue(html.contains("src=\"homecoming.js\""));
        assertFalse(html.contains("/static/"));
    }

    @Test
    public void assetsContainNoNetworkEndpointOrSecretField() throws Exception {
        StringBuilder assets = new StringBuilder();
        Path directory = Paths.get("src/main/assets/homecoming");
        for (String name : Arrays.asList("index.html", "homecoming.css", "homecoming.js")) {
            assets.append(read(directory.resolve(name).toString()));
        }
        String source = assets.toString();
        assertFalse(source.contains("http://"));
        assertFalse(source.contains("https://"));
        assertFalse(source.contains("api_key"));
        assertFalse(source.contains("saved_url"));
    }

    @Test
    public void bridgeExposesOnlyTheTypedHomecomingSurfaceToJavascript() {
        Set<String> methods = new HashSet<>();
        for (Method method : HomecomingBridge.class.getDeclaredMethods()) {
            if (method.getAnnotation(JavascriptInterface.class) != null) {
                methods.add(method.getName());
            }
        }
        assertEquals(new HashSet<>(Arrays.asList(
                "getReadinessJson",
                "requestRefresh",
                "confirmEnter",
                "cancelEnter",
                "requestFoundationReturn",
                "startReturnSync",
                "retryReturnSync",
                "returnWithoutSync",
                "getReturnStateJson",
                "isActive",
                "getBootstrapJson",
                "getSupervisionStatusJson",
                "getMessagesJson",
                "sendMessage",
                "stopMessage",
                "setTtsEnabled",
                "setRoutePreference",
                "replayTts",
                "getMemoriesJson",
                "summarizeMemories",
                "summarizeAllMemories",
                "createMemory",
                "updateMemory",
                "deleteMemory",
                "listSchedules",
                "createSchedule",
                "deleteSchedule",
                "pickImage",
                "captureImage")), methods);
    }

    @Test
    public void returnPanelUsesOnlyTypedManualActionsAndTextContent()
            throws Exception {
        String html = read("src/main/assets/homecoming/index.html");
        String script = read("src/main/assets/homecoming/homecoming.js");
        assertTrue(html.contains("id=\"returnPanel\""));
        assertTrue(script.contains("startReturnSync()"));
        assertTrue(script.contains("retryReturnSync()"));
        assertTrue(script.contains("returnWithoutSync()"));
        assertTrue(script.contains("getReturnStateJson()"));
        assertFalse(script.contains("innerHTML"));
        assertFalse(script.contains("setInterval("));
    }

    private static String read(String path) throws Exception {
        return new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8);
    }
}
