package com.aion.chat.homecoming;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

public final class HomecomingTtsEngine {
    private final Synthesizer synthesizer;
    private final Player player;
    private final int segmentLimit;
    private final Deque<Batch> batches = new ArrayDeque<>();
    private boolean playing;

    public HomecomingTtsEngine(
            Synthesizer synthesizer, Player player, int segmentLimit) {
        this.synthesizer = synthesizer;
        this.player = player;
        if (segmentLimit < 4) throw new IllegalArgumentException("segment limit is too small");
        this.segmentLimit = segmentLimit;
    }

    public synchronized void enqueue(
            String timelineId, String messageId, String voiceId, String text) {
        String speakable = HomecomingControlParser.parse(text).visibleText;
        List<String> segments = segment(speakable, segmentLimit);
        if (segments.isEmpty()) return;
        Batch batch = new Batch(timelineId, messageId, segments);
        batches.addLast(batch);
        for (int index = 0; index < segments.size(); index++) {
            final int slotIndex = index;
            synthesizer.synthesize(segments.get(index), voiceId, new SynthesisCallback() {
                @Override public void onSuccess(byte[] audio) {
                    completeSlot(batch, slotIndex, audio, false);
                }
                @Override public void onFailure() {
                    completeSlot(batch, slotIndex, null, true);
                }
            });
        }
    }

    public synchronized void cancelConversation(String timelineId) {
        for (Batch batch : batches) {
            if (batch.timelineId.equals(timelineId)) batch.cancelled = true;
        }
        if (playing) player.stop();
        playing = false;
        removeFinished();
        drain();
    }

    private synchronized void completeSlot(
            Batch batch, int index, byte[] audio, boolean failed) {
        if (batch.cancelled) return;
        Slot slot = batch.slots.get(index);
        slot.audio = audio;
        slot.failed = failed;
        slot.ready = true;
        drain();
    }

    private void drain() {
        if (playing) return;
        removeFinished();
        Batch batch = batches.peekFirst();
        if (batch == null || batch.cancelled) {
            removeFinished();
            return;
        }
        if (batch.nextIndex >= batch.slots.size()) {
            batches.removeFirst();
            drain();
            return;
        }
        Slot slot = batch.slots.get(batch.nextIndex);
        if (!slot.ready) return;
        batch.nextIndex++;
        if (slot.failed || slot.audio == null) {
            drain();
            return;
        }
        playing = true;
        player.play(slot.audio, () -> {
            synchronized (HomecomingTtsEngine.this) {
                playing = false;
                drain();
            }
        });
    }

    private void removeFinished() {
        while (!batches.isEmpty()) {
            Batch first = batches.peekFirst();
            if (!first.cancelled && first.nextIndex < first.slots.size()) break;
            batches.removeFirst();
        }
    }

    static List<String> segment(String text, int limit) {
        ArrayList<String> result = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char value = text.charAt(i);
            current.append(value);
            boolean boundary = "。！？!?；;\n".indexOf(value) >= 0;
            if (boundary || current.length() >= limit) {
                String segment = current.toString().trim();
                if (!segment.isEmpty()) result.add(segment);
                current.setLength(0);
            }
        }
        String tail = current.toString().trim();
        if (!tail.isEmpty()) result.add(tail);
        return result;
    }

    public interface Synthesizer {
        void synthesize(String text, String voiceId, SynthesisCallback callback);
    }

    public interface SynthesisCallback {
        void onSuccess(byte[] audio);
        void onFailure();
    }

    public interface Player {
        void play(byte[] audio, Runnable completion);
        void stop();
    }

    private static final class Batch {
        final String timelineId;
        final String messageId;
        final List<Slot> slots = new ArrayList<>();
        int nextIndex;
        boolean cancelled;
        Batch(String timelineId, String messageId, List<String> segments) {
            this.timelineId = timelineId;
            this.messageId = messageId;
            for (int i = 0; i < segments.size(); i++) slots.add(new Slot());
        }
    }

    private static final class Slot {
        byte[] audio;
        boolean ready;
        boolean failed;
    }
}
