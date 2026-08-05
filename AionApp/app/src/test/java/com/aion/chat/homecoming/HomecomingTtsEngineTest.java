package com.aion.chat.homecoming;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class HomecomingTtsEngineTest {
    @Test
    public void outOfOrderSynthesisStillPlaysInSentenceOrder() {
        FakeSynthesizer synth = new FakeSynthesizer();
        RecordingPlayer player = new RecordingPlayer();
        HomecomingTtsEngine engine = new HomecomingTtsEngine(synth, player, 8);

        engine.enqueue("main_private", "message-one", "voice-main", "第一句。第二句！");
        synth.complete(1);
        assertTrue(player.played.isEmpty());
        synth.complete(0);

        assertEquals(java.util.Arrays.asList("第一句。", "第二句！"), player.played);
        assertEquals(java.util.Arrays.asList("voice-main", "voice-main"), synth.voices);
    }

    @Test
    public void controlTagsAreNeverSpokenAndCancellationDropsPendingSegments() {
        FakeSynthesizer synth = new FakeSynthesizer();
        RecordingPlayer player = new RecordingPlayer();
        HomecomingTtsEngine engine = new HomecomingTtsEngine(synth, player, 20);

        engine.enqueue("group", "message-two", "voice-second",
                "好的[ALARM:2030-01-02T08:00|起床]。稍后见。");
        assertTrue(synth.texts.toString().indexOf("ALARM") < 0);
        engine.cancelConversation("group");
        synth.completeAll();

        assertTrue(player.played.isEmpty());
    }

    @Test
    public void synthesisFailureDoesNotRequestChatAgainOrBlockLaterText() {
        FakeSynthesizer synth = new FakeSynthesizer();
        RecordingPlayer player = new RecordingPlayer();
        HomecomingTtsEngine engine = new HomecomingTtsEngine(synth, player, 8);

        engine.enqueue("main_private", "message-three", "voice-main", "失败句。后一句。");
        synth.fail(0);
        synth.complete(1);

        assertEquals(java.util.Collections.singletonList("后一句。"), player.played);
    }

    private static final class FakeSynthesizer implements HomecomingTtsEngine.Synthesizer {
        final List<String> texts = new ArrayList<>();
        final List<String> voices = new ArrayList<>();
        final Map<Integer, HomecomingTtsEngine.SynthesisCallback> callbacks =
                new LinkedHashMap<>();
        @Override
        public void synthesize(String text, String voice,
                HomecomingTtsEngine.SynthesisCallback callback) {
            int index = texts.size();
            texts.add(text);
            voices.add(voice);
            callbacks.put(index, callback);
        }
        void complete(int index) {
            callbacks.get(index).onSuccess(
                    texts.get(index).getBytes(StandardCharsets.UTF_8));
        }
        void fail(int index) { callbacks.get(index).onFailure(); }
        void completeAll() {
            for (Integer index : new ArrayList<>(callbacks.keySet())) complete(index);
        }
    }

    private static final class RecordingPlayer implements HomecomingTtsEngine.Player {
        final List<String> played = new ArrayList<>();
        @Override public void play(byte[] audio, Runnable completion) {
            played.add(new String(audio, StandardCharsets.UTF_8));
            completion.run();
        }
        @Override public void stop() { }
    }
}
