package com.aion.chat;

import org.junit.Test;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import static org.junit.Assert.*;

public class TtsPlaybackCoordinatorTest {
    @Test public void listeningStaysPausedAcrossChunksAndQueuedPlayers() {
        TtsPlaybackCoordinator coordinator = new TtsPlaybackCoordinator();
        List<Boolean> states = new ArrayList<>();
        coordinator.addListener(states::add);
        Object first = new Object(), second = new Object();
        coordinator.request(first, () -> assertTrue(coordinator.isSpeaking()));
        coordinator.request(first, () -> {});
        coordinator.request(second, () -> {});
        coordinator.release(first);
        assertEquals(Arrays.asList(false, true), states);
        coordinator.release(second);
        assertEquals(Arrays.asList(false, true, false), states);
    }
    @Test public void messageContinuesAcrossChunksBeforeReturningToTheater() {
        TtsPlaybackCoordinator coordinator = new TtsPlaybackCoordinator();
        Object theater = new Object(), message = new Object();
        List<String> started = new ArrayList<>();
        coordinator.request(theater, () -> started.add("theater-0"));
        coordinator.request(message, () -> started.add("message-0"));
        coordinator.release(theater);
        coordinator.request(theater, () -> started.add("theater-1"));
        // The message keeps ownership even while its next chunk is being synthesized.
        coordinator.request(message, () -> started.add("message-1"));
        assertEquals(Arrays.asList("theater-0", "message-0", "message-1"), started);
        coordinator.release(message);
        assertEquals(Arrays.asList("theater-0", "message-0", "message-1", "theater-1"), started);
    }

    @Test public void backgroundWakeupWaitsForForegroundSpeechAndReturnAlsoWaits() {
        TtsPlaybackCoordinator coordinator = new TtsPlaybackCoordinator();
        Object foreground = new Object(), background = new Object(), returned = new Object();
        List<String> started = new ArrayList<>();
        coordinator.request(foreground, () -> started.add("foreground"));
        coordinator.request(background, () -> started.add("background"));
        assertEquals(Arrays.asList("foreground"), started);
        coordinator.release(foreground);
        coordinator.request(returned, () -> started.add("returned"));
        assertEquals(Arrays.asList("foreground", "background"), started);
        coordinator.release(foreground); // A late callback must not free another player.
        assertEquals(2, started.size());
        coordinator.release(background);
        assertEquals(Arrays.asList("foreground", "background", "returned"), started);
    }

    @Test public void stoppedQueuedSpeechNeverStartsAndErrorsFreeNextPlayer() {
        TtsPlaybackCoordinator coordinator = new TtsPlaybackCoordinator();
        Object playing = new Object(), cancelled = new Object(), next = new Object();
        List<String> started = new ArrayList<>();
        coordinator.request(playing, () -> started.add("playing"));
        coordinator.request(cancelled, () -> started.add("cancelled"));
        coordinator.request(next, () -> started.add("next"));
        coordinator.request(next, () -> started.add("duplicate"));
        coordinator.release(cancelled);
        assertEquals(Arrays.asList("playing"), started);
        coordinator.release(playing);
        assertEquals(Arrays.asList("playing", "next"), started);
    }
}
