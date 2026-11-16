package com.aion.chat;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class BackgroundTtsNetworkResetTest {
    @Test public void foregroundSettingsReportKeepsNativeSpeechUntilExplicitStop() throws Exception {
        AionPushService service = new AionPushService();
        BackgroundTtsPlayer player = new BackgroundTtsPlayer(null, null, () -> {});
        field(AionPushService.class, "backgroundTtsPlayer").set(service, player);
        BackgroundTtsQueue queue = (BackgroundTtsQueue) field(BackgroundTtsPlayer.class, "queue").get(player);
        queue.offer("recall", 0, "/first");
        queue.offer("recall", 1, "/second");
        service.onBackgroundTtsPreferenceChanged("background_tts_updated");
        assertEquals("/first", queue.next());
        queue.advance();
        assertEquals("/second", queue.next());
        service.onBackgroundTtsPreferenceChanged("background_tts_stop");
        assertFalse(player.hasPending());
    }

    @Test public void networkResetReleasesMissingDoneAndUnblocksForegroundSpeech() throws Exception {
        AionPushService service = new AionPushService();
        BackgroundTtsPlayer player = new BackgroundTtsPlayer(null, null, () -> {});
        BackgroundTtsQueue queue = (BackgroundTtsQueue) field(BackgroundTtsPlayer.class, "queue").get(player);
        Object interrupted = new Object(), foreground = new Object();
        List<String> started = new ArrayList<>();
        field(AionPushService.class, "backgroundTtsPlayer").set(service, player);
        field(BackgroundTtsPlayer.class, "speechOwner").set(player, interrupted);
        field(BackgroundTtsPlayer.class, "speechMessageId").set(player, "interrupted");
        // A clip has finished, but its tts_done was lost when Wi-Fi switched.
        queue.offer("interrupted", 0, "/first");
        queue.advance();
        try {
            TtsPlaybackCoordinator.shared.request(interrupted, () -> started.add("background"));
            TtsPlaybackCoordinator.shared.request(foreground, () -> started.add("foreground"));
            Method reset = AionPushService.class.getDeclaredMethod("resetWebSocketForEndpointChange");
            reset.setAccessible(true);
            reset.invoke(service);

            assertFalse("A network reset must finish the abandoned message", player.hasPending());
            assertEquals(Arrays.asList("background", "foreground"), started);
            queue.offer("next-reply", 0, "/next");
            assertEquals("/next", queue.next());
        } finally {
            queue.clear();
            player.stop();
            TtsPlaybackCoordinator.shared.release(interrupted);
            TtsPlaybackCoordinator.shared.release(foreground);
        }
    }

    private static Field field(Class<?> type, String name) throws Exception {
        Field field = type.getDeclaredField(name);
        field.setAccessible(true);
        return field;
    }
}
