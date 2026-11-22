package com.aion.chat;

import org.json.JSONArray;
import org.json.JSONObject;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;

/** Replay checkpoint and bounded dedup; the push service serializes state changes under its lock. */
final class PushMessageSync {
    interface Delivery { void accept(JSONObject event, boolean notify) throws Exception; }

    private long cursor = -1;
    private boolean replayComplete;
    private final LinkedHashMap<String, Long> versions = new LinkedHashMap<>();
    private final LinkedHashSet<String> notified = new LinkedHashSet<>();

    PushMessageSync(String saved) {
        try {
            JSONObject state = new JSONObject(saved);
            cursor = state.optLong("cursor", -1);
            JSONObject savedVersions = state.optJSONObject("versions");
            if (savedVersions != null) {
                Iterator<String> keys = savedVersions.keys();
                while (keys.hasNext()) { String key = keys.next(); versions.put(key, savedVersions.optLong(key)); }
            }
            JSONArray savedNotified = state.optJSONArray("notified");
            if (savedNotified != null) for (int i = 0; i < savedNotified.length(); i++) notified.add(savedNotified.getString(i));
        } catch (Exception ignored) { }
        trim();
    }

    /** Returns the saved snapshot with the furthest cursor. */
    static String newest(String... saved) {
        String best = "";
        long bestCursor = -1;
        for (String value : saved) {
            long cursor = new PushMessageSync(value == null ? "" : value).cursor;
            if (cursor > bestCursor) { best = value; bestCursor = cursor; }
        }
        return best;
    }

    long cursor() { return Math.max(0, cursor); }
    boolean initialized() { return cursor >= 0; }
    boolean needsReplay() { return !replayComplete; }
    void beginReplay() { replayComplete = false; }
    void finishReplay() { replayComplete = true; }

    void resetTo(long latest) {
        // A baseline HTTP response may arrive after newer live events. Keep their
        // bounded dedup records; only a server sequence rewind starts a new history.
        if (latest < cursor) {
            versions.clear();
            notified.clear();
        }
        cursor = Math.max(0, latest);
    }

    void acceptLive(JSONObject event, Delivery delivery) throws Exception {
        long seq = event.optLong("sync_seq");
        if (seq > cursor + 1) replayComplete = false;
        accept(event, false, delivery);
    }

    void acceptReplay(JSONObject event, Delivery delivery) throws Exception {
        accept(event, true, delivery);
    }

    private void accept(JSONObject event, boolean replay, Delivery delivery) throws Exception {
        long seq = event.optLong("sync_seq");
        if (seq <= cursor || seq <= 0) return;
        String type = event.optString("type");
        JSONObject data = event.optJSONObject("data");
        if (type.matches("(chatroom_)?msg_(created|updated|deleted)") && data != null && !data.optString("id").isEmpty()) {
            String key = (type.startsWith("chatroom_") ? "room:" : "chat:")
                    + data.optString("room_id", data.optString("conv_id")) + ":" + data.optString("id");
            if (seq > versions.getOrDefault(key, 0L)) {
                boolean notify = type.endsWith("_created") && !notified.contains(key);
                delivery.accept(event, notify);
                versions.remove(key);
                versions.put(key, seq);
                if (notify) notified.add(key);
                trim();
            }
        }
        // Live traffic may advance the checkpoint only after the missing prefix is complete.
        if (replay || replayComplete) cursor = seq;
    }

    private void trim() {
        while (versions.size() > 512) versions.remove(versions.keySet().iterator().next());
        while (notified.size() > 200) notified.remove(notified.iterator().next());
    }

    String snapshot() throws Exception {
        return new JSONObject().put("cursor", cursor).put("versions", new JSONObject(versions))
                .put("notified", new JSONArray(notified)).toString();
    }
}
