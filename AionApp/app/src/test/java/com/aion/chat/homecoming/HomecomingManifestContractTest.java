package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import static org.junit.Assert.assertTrue;

public class HomecomingManifestContractTest {
    @Test
    public void registersOnlyHomecomingOwnedBackgroundComponents() throws Exception {
        String manifest = new String(Files.readAllBytes(
                Paths.get("src/main/AndroidManifest.xml")), StandardCharsets.UTF_8);

        assertTrue(manifest.contains(
                "android:name=\".homecoming.HomecomingAlarmReceiver\""));
        assertTrue(manifest.contains(
                "android:name=\".homecoming.HomecomingBootReceiver\""));
        assertTrue(manifest.contains(
                "android:name=\".homecoming.HomecomingForegroundService\""));
        assertTrue(manifest.contains("android.intent.action.BOOT_COMPLETED"));
        assertTrue(manifest.contains("android.intent.action.TIME_SET"));
        assertTrue(manifest.contains("android.intent.action.TIMEZONE_CHANGED"));
        assertTrue(manifest.contains("android:exported=\"false\""));
        assertTrue(manifest.contains("android.permission.RECEIVE_BOOT_COMPLETED"));
    }
}
