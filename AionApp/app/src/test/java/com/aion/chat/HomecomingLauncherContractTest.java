package com.aion.chat;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingLauncherContractTest {
    @Test
    public void launcherContainsFourthHomecomingButtonAndExactSubtitle() throws Exception {
        String xml = read("src/main/res/layout/activity_launcher.xml");
        assertTrue(xml.contains("@+id/btnHomecoming"));
        assertTrue(xml.contains("归巢模式"));
        assertTrue(xml.contains("家庭服务器不可用时使用"));
        assertFalse(xml.contains("手机本地灾备"));
    }

    @Test
    public void homecomingClickDoesNotUseNormalLaunchMethods() throws Exception {
        String source = read("src/main/java/com/aion/chat/LauncherActivity.java");
        String block = listenerBlock(source, "btnHomecoming");
        assertTrue(block.contains("HomecomingActivity.class"));
        assertFalse(block.contains("saveIfNeeded("));
        assertFalse(block.contains("launchWebView("));
        assertFalse(block.contains("startPushService("));
    }

    @Test
    public void normalThreeButtonsKeepExistingLaunchWebViewPath() throws Exception {
        String source = read("src/main/java/com/aion/chat/LauncherActivity.java");
        assertTrue(listenerBlock(source, "btnHome").contains("launchWebView(URL_HOME)"));
        assertTrue(listenerBlock(source, "btnCloudflare")
                .contains("launchWebView(URL_CLOUDFLARE)"));
        assertTrue(listenerBlock(source, "btnOutdoor")
                .contains("launchWebView(URL_OUTDOOR)"));
    }

    @Test
    public void manifestKeepsHomecomingActivityPrivate() throws Exception {
        String manifest = read("src/main/AndroidManifest.xml");
        int start = manifest.indexOf("android:name=\".homecoming.HomecomingActivity\"");
        assertTrue(start >= 0);
        String block = manifest.substring(start, Math.min(manifest.length(), start + 250));
        assertTrue(block.contains("android:exported=\"false\""));
    }

    @Test
    public void pendingReturnOnlyDecoratesHomecomingEntry() throws Exception {
        String source = read("src/main/java/com/aion/chat/LauncherActivity.java");
        assertTrue(source.contains("pendingInSequence()"));
        assertTrue(source.contains("EXTRA_OPEN_RETURN"));
        assertFalse(listenerBlock(source, "btnHome").contains("pendingInSequence"));
        assertFalse(listenerBlock(source, "btnCloudflare").contains("pendingInSequence"));
        assertFalse(listenerBlock(source, "btnOutdoor").contains("pendingInSequence"));
    }

    private static String listenerBlock(String source, String variable) {
        String marker = variable + ".setOnClickListener";
        int start = source.indexOf(marker);
        if (start < 0) {
            return "";
        }
        int end = source.indexOf("});", start);
        return end < 0 ? source.substring(start) : source.substring(start, end + 3);
    }

    private static String read(String path) throws Exception {
        return new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8);
    }
}
