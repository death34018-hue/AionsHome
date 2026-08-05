package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingSupervisionBackgroundContractTest {
    @Test
    public void enteringStartsOnlyHomecomingOwnedBackgroundService() throws Exception {
        String activity = source("HomecomingActivity.java");

        assertTrue(activity.contains("startHomecomingService();"));
        assertTrue(activity.contains(
                "stopService(new Intent(this, AionPushService.class));"));
        assertFalse(activity.contains(
                "startService(new Intent(this, AionPushService.class))"));
        assertFalse(activity.contains(
                "startForegroundService(new Intent(this, AionPushService.class))"));
    }

    @Test
    public void bootRestoresLongLivedListenerAndDestroyDetachesIt() throws Exception {
        String service = source("HomecomingForegroundService.java");
        String receiver = source("HomecomingBootReceiver.java");

        assertTrue(service.contains("public static final String ACTION_START"));
        assertTrue(service.contains("supervisionController.start();"));
        assertTrue(service.contains("supervisionController.stop();"));
        assertTrue(service.contains("return START_STICKY;"));
        assertTrue(receiver.contains(
                "setAction(HomecomingForegroundService.ACTION_START)"));
    }

    @Test
    public void returnSyncBeginsFreezeBeforeStoppingItsBackgroundService() throws Exception {
        String activity = source("HomecomingActivity.java");

        int freeze = activity.indexOf("modeStore.beginFreezing();");
        int freezeRuntime = activity.indexOf("freezeRuntime();", freeze);
        int stop = activity.indexOf(
                "stopService(new Intent(this, HomecomingForegroundService.class));",
                activity.indexOf("private void freezeRuntime()"));
        assertTrue(freeze >= 0);
        assertTrue(freezeRuntime > freeze);
        assertTrue(stop > freezeRuntime);
    }

    private static String source(String name) throws Exception {
        return new String(
                Files.readAllBytes(Paths.get(
                        "src/main/java/com/aion/chat/homecoming/" + name)),
                StandardCharsets.UTF_8);
    }
}
