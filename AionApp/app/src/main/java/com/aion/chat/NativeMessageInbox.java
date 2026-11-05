package com.aion.chat;

import android.content.Context;
import org.json.JSONArray;
import org.json.JSONObject;
import java.net.URI;

/** Bounded durable hand-off from the push socket to the visible chat. */
final class NativeMessageInbox {
    private static String key(String url) {
        try { return "native_messages_v1_" + URI.create(url).getAuthority(); }
        catch (Exception ignored) { return "native_messages_v1_unknown"; }
    }

    static synchronized String read(Context context, String url) {
        return new SharedJsonStore(context).get(key(url));
    }

    static synchronized boolean receive(Context context, String url, JSONObject event) {
        String type = event.optString("type");
        if (!type.matches("(chatroom_)?msg_(created|updated|deleted)")) return false;
        JSONObject data = event.optJSONObject("data");
        if (data == null || data.optString("id").isEmpty()) return false;
        try {
            SharedJsonStore store = new SharedJsonStore(context);
            String saved = store.get(key(url));
            JSONArray events = saved.isEmpty() ? new JSONArray() : new JSONArray(saved);
            long seq = events.length() == 0 ? 0 : events.getJSONObject(events.length() - 1).optLong("native_seq");
            JSONObject copy = new JSONObject(event.toString());
            copy.put("native_seq", Math.max(System.currentTimeMillis(), seq + 1));
            events.put(copy);
            while (events.length() > 128 || (events.length() > 1 && events.toString().length() > 240000)) events.remove(0);
            store.put(key(url), events.toString());
            return true;
        } catch (Exception ignored) { return false; }
    }
}
