package com.aion.chat;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import org.junit.Test;

import static org.junit.Assert.assertTrue;

public class SupervisionPackagingGuardTest {
    @Test
    public void manifestKeepsRecoveryAndOverlayPermissions() throws Exception {
        String xml = new String(Files.readAllBytes(
                Paths.get("src/main/AndroidManifest.xml")), StandardCharsets.UTF_8);
        assertTrue(xml.contains("android.permission.WRITE_SECURE_SETTINGS"));
        assertTrue(xml.contains("android.permission.SYSTEM_ALERT_WINDOW"));
        assertTrue(xml.contains("android.intent.category.LAUNCHER"));
    }

    @Test
    public void installScriptAlwaysGrantsAndVerifiesSecureSettings() throws Exception {
        String script = new String(Files.readAllBytes(
                Paths.get("../scripts/install-debug-with-accessibility.ps1")),
                StandardCharsets.UTF_8);
        assertTrue(script.contains("install -r"));
        assertTrue(script.contains(
                "pm grant com.aion.chat android.permission.WRITE_SECURE_SETTINGS"));
        assertTrue(script.contains("dumpsys package com.aion.chat"));
    }
}
