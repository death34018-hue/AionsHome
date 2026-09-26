package com.aion.chat;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.TreeMap;

/** Main-thread queue for the same segmented speech protocol used by the chat pages. */
final class BackgroundTtsQueue {
    private static final class Message {
        final TreeMap<Integer, String> chunks = new TreeMap<>();
        int next;
        boolean finished;
    }
    private final LinkedHashMap<String, Message> messages = new LinkedHashMap<>();
    private final LinkedHashSet<String> completed = new LinkedHashSet<>();

    void offer(String id, int seq, String url) {
        if (id.isEmpty() || seq < 0 || url.isEmpty() || completed.contains(id)) return;
        Message message = messages.get(id);
        if (message == null) {
            message = new Message();
            messages.put(id, message);
        }
        if (seq >= message.next) message.chunks.putIfAbsent(seq, url);
    }

    void finish(String id) {
        Message message = messages.get(id);
        if (message != null) message.finished = true;
    }

    void finishPending() {
        for (Message message : messages.values()) message.finished = true;
        next();
    }

    boolean contains(String id) { return messages.containsKey(id); }

    String next() {
        while (!messages.isEmpty()) {
            String id = messages.keySet().iterator().next();
            Message message = messages.get(id);
            String url = message.chunks.get(message.next);
            if (url != null || !message.finished) return url;
            Integer remaining = message.chunks.ceilingKey(message.next);
            if (remaining != null) {
                message.next = remaining;
                return message.chunks.get(remaining);
            }
            messages.remove(id);
            remember(id);
        }
        return null;
    }

    void advance() {
        if (messages.isEmpty()) return;
        Message message = messages.values().iterator().next();
        message.chunks.remove(message.next++);
        next();
    }

    boolean hasPending() {
        next();
        return !messages.isEmpty();
    }

    void clear() {
        for (String id : messages.keySet()) remember(id);
        messages.clear();
    }

    private void remember(String id) {
        completed.add(id);
        while (completed.size() > 64) completed.remove(completed.iterator().next());
    }
}
