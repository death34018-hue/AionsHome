package com.aion.chat.miband;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

/** In-memory delivery state; the server remains the durable source of truth. */
public final class MiBandCommandInbox {
    public static final class Command {
        public final String id;
        public final String pattern;
        public final String note;
        public final String senderName;
        public final long expiresAtMillis;
        private boolean inFlight;

        Command(String id, String pattern, String note, String senderName, long expiresAtMillis) {
            this.id = id;
            this.pattern = pattern;
            this.note = note == null ? "" : note;
            this.senderName = senderName == null ? "" : senderName;
            this.expiresAtMillis = expiresAtMillis;
        }
    }

    private final Map<String, Command> pending = new LinkedHashMap<>();
    private final Set<String> completed = new LinkedHashSet<>();

    public synchronized boolean offer(String id, String pattern, String note,
                                      String senderName, long expiresAtMillis) {
        if (id == null || id.trim().isEmpty()) return false;
        if (!"single".equals(pattern) && !"call".equals(pattern)) return false;
        if (pending.containsKey(id) || completed.contains(id)) return false;
        pending.put(id, new Command(id, pattern, note, senderName, expiresAtMillis));
        return true;
    }

    public synchronized Command nextReady(long nowMillis, boolean authenticated) {
        Iterator<Map.Entry<String, Command>> iterator = pending.entrySet().iterator();
        while (iterator.hasNext()) {
            Command command = iterator.next().getValue();
            if (command.expiresAtMillis <= nowMillis) iterator.remove();
        }
        if (!authenticated) return null;
        for (Command command : pending.values()) {
            if (!command.inFlight) {
                command.inFlight = true;
                return command;
            }
        }
        return null;
    }

    public synchronized void complete(String id, boolean success) {
        Command command = pending.get(id);
        if (command == null) return;
        if (success) {
            pending.remove(id);
            completed.add(id);
            while (completed.size() > 100) {
                Iterator<String> iterator = completed.iterator();
                if (iterator.hasNext()) {
                    iterator.next();
                    iterator.remove();
                }
            }
        } else {
            command.inFlight = false;
        }
    }

    public synchronized int pendingCount() { return pending.size(); }
}
