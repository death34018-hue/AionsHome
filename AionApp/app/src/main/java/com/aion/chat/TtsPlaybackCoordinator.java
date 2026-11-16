package com.aion.chat;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/** Main-thread speech ownership shared by the WebView and push service players. */
final class TtsPlaybackCoordinator {
    static final TtsPlaybackCoordinator shared = new TtsPlaybackCoordinator();

    private static final class Pending {
        final Object owner;
        final Runnable start;
        Pending(Object owner, Runnable start) { this.owner = owner; this.start = start; }
    }

    private Object active;
    private final ArrayDeque<Pending> pending = new ArrayDeque<>();
    interface Listener { void onSpeakingChanged(boolean speaking); }
    private final List<Listener> listeners = new ArrayList<>();
    private volatile boolean speaking;

    boolean isSpeaking() { return speaking; }
    void addListener(Listener listener) {
        listeners.add(listener);
        listener.onSpeakingChanged(speaking);
    }
    void removeListener(Listener listener) { listeners.remove(listener); }

    private void updateSpeaking() {
        boolean next = active != null;
        if (speaking == next) return;
        speaking = next;
        for (Listener listener : new ArrayList<>(listeners)) listener.onSpeakingChanged(next);
    }

    void request(Object owner, Runnable start) {
        if (active == owner) {
            start.run(); // Continue the same message without yielding between its clips.
            return;
        }
        for (Pending item : pending) if (item.owner == owner) return;
        pending.addLast(new Pending(owner, start));
        startNext();
    }

    void release(Object owner) {
        pending.removeIf(item -> item.owner == owner);
        if (active == owner) active = null;
        startNext();
        updateSpeaking();
    }

    private void startNext() {
        if (active != null || pending.isEmpty()) return;
        Pending item = pending.removeFirst();
        active = item.owner;
        updateSpeaking();
        item.start.run();
    }
}
