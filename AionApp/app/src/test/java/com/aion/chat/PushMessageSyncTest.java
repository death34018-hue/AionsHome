package com.aion.chat;

import org.json.JSONObject;
import org.junit.Test;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class PushMessageSyncTest {
    private JSONObject message(long seq, String type, String id, String content) throws Exception {
        return new JSONObject().put("sync_seq", seq).put("type", type)
                .put("data", new JSONObject().put("id", id).put("conv_id", "c")
                        .put("role", "assistant").put("content", content));
    }

    @Test public void failedReplayCannotLetNewLiveMessagesSkipTheMissingHistory() throws Exception {
        PushMessageSync sync = new PushMessageSync("{\"cursor\":100}");
        List<String> delivered = new ArrayList<>();
        PushMessageSync.Delivery sink = (event, notify) -> delivered.add(event.getJSONObject("data").getString("id"));
        sync.acceptLive(message(103, "msg_created", "new", "new"), sink);
        assertEquals(100, sync.cursor());
        assertTrue(sync.needsReplay());
        sync.acceptReplay(message(101, "msg_created", "missed-1", "one"), sink);
        sync.acceptReplay(message(102, "msg_created", "missed-2", "two"), sink);
        sync.acceptReplay(message(103, "msg_created", "new", "new"), sink);
        sync.finishReplay();
        assertEquals(103, sync.cursor());
        assertEquals(java.util.Arrays.asList("new", "missed-1", "missed-2"), delivered);
    }

    @Test public void replayCannotRollBackLiveEditsAndNotificationDedupSurvivesRestart() throws Exception {
        PushMessageSync sync = new PushMessageSync("{\"cursor\":10}");
        List<String> delivered = new ArrayList<>();
        List<String> notifications = new ArrayList<>();
        PushMessageSync.Delivery sink = (event, notify) -> {
            delivered.add(event.getJSONObject("data").getString("content"));
            if (notify) notifications.add(event.getJSONObject("data").getString("id"));
        };
        sync.acceptLive(message(12, "msg_updated", "m", "edited"), sink);
        sync.acceptReplay(message(11, "msg_created", "m", "old"), sink);
        sync.acceptReplay(message(12, "msg_updated", "m", "edited"), sink);
        sync.acceptReplay(message(13, "msg_created", "n", "new"), sink);
        assertEquals(java.util.Arrays.asList("edited", "new"), delivered);
        assertEquals(java.util.Arrays.asList("n"), notifications);
        PushMessageSync restarted = new PushMessageSync(sync.snapshot());
        restarted.acceptReplay(message(14, "msg_created", "n", "new"), sink);
        restarted.acceptReplay(message(15, "msg_deleted", "m", "deleted"), sink);
        assertEquals(1, notifications.size());
        assertEquals("deleted", delivered.get(delivered.size() - 1));
    }

    @Test public void firstBaselineDoesNotNotifyHistoryAndDeliveryFailureDoesNotAdvanceCursor() throws Exception {
        PushMessageSync sync = new PushMessageSync("");
        assertFalse(sync.initialized());
        sync.resetTo(50);
        sync.acceptReplay(message(49, "msg_created", "old", "old"), (event, notify) -> fail("historical notification"));
        try {
            sync.acceptReplay(message(51, "msg_created", "next", "next"), (event, notify) -> { throw new Exception("offline storage"); });
            fail("expected delivery error");
        } catch (Exception expected) { assertEquals("offline storage", expected.getMessage()); }
        assertEquals(50, sync.cursor());
        sync.finishReplay();
        sync.acceptLive(message(52, "msg_created", "gap", "gap"), (event, notify) -> {});
        assertTrue(sync.needsReplay());
        assertEquals(50, sync.cursor());
    }

    @Test public void migrationKeepsTheNewestAddressCheckpoint() throws Exception {
        String home = new PushMessageSync("{\"cursor\":500}").snapshot();
        String outdoor = new PushMessageSync("{\"cursor\":120}").snapshot();
        PushMessageSync merged = new PushMessageSync(PushMessageSync.newest(outdoor, "", null, home));
        assertEquals(500, merged.cursor());
        merged.acceptReplay(message(300, "msg_created", "seen-at-home", "old"),
                (event, notify) -> fail("message already received on another address"));
        assertEquals("", PushMessageSync.newest("", null));
    }

    @Test public void baselineResponseCannotEraseNewerLiveNotificationDedup() throws Exception {
        PushMessageSync sync = new PushMessageSync("");
        List<String> notifications = new ArrayList<>();
        PushMessageSync.Delivery sink = (event, notify) -> {
            if (notify) notifications.add(event.getJSONObject("data").getString("id"));
        };
        sync.acceptLive(message(101, "msg_created", "new", "new"), sink);
        sync.resetTo(100); // HTTP snapshot was captured before the live message.
        sync.acceptReplay(message(101, "msg_created", "new", "new"), sink);
        assertEquals(101, sync.cursor());
        assertEquals(java.util.Arrays.asList("new"), notifications);
    }
}
