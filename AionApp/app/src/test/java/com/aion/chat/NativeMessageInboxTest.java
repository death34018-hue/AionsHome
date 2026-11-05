package com.aion.chat;

import android.content.Context;
import android.content.ContextWrapper;
import java.io.File;
import java.nio.file.Files;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class NativeMessageInboxTest {
    @Test public void persistedInboxPreservesEditsAndDeletesAcrossPageResumeWithoutCrossingServers() throws Exception {
        File root = Files.createTempDirectory("aion-message-inbox").toFile();
        Context context = new ContextWrapper(null) {
            @Override public Context getApplicationContext() { return this; }
            @Override public File getFilesDir() { return root; }
        };
        try {
            for (String kind : new String[] { "created", "updated", "deleted" }) {
                assertTrue(NativeMessageInbox.receive(context, "wss://home.test/ws", new JSONObject()
                    .put("type", "chatroom_msg_" + kind).put("sync_seq", 10)
                    .put("data", new JSONObject().put("room_id", "r").put("id", "m"))));
            }
            JSONArray saved = new JSONArray(NativeMessageInbox.read(context, "https://home.test/chat"));
            assertEquals(3, saved.length());
            assertEquals("chatroom_msg_deleted", saved.getJSONObject(2).getString("type"));
            assertTrue(saved.getJSONObject(2).getLong("native_seq") > saved.getJSONObject(0).getLong("native_seq"));
            assertEquals("", NativeMessageInbox.read(context, "https://other.test/chat"));
            assertFalse(NativeMessageInbox.receive(context, "wss://home.test/ws", new JSONObject().put("type", "tts_chunk")));
        } finally {
            File dir = new File(root, "shared-json-v1");
            for (File file : dir.listFiles()) file.delete(); dir.delete(); root.delete();
        }
    }
}
