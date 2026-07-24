package com.aion.chat.miband;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

public class MiBandCommandInboxTest {
    @Test public void disconnectedCommandWaitsUntilAuthenticated() {
        MiBandCommandInbox inbox = new MiBandCommandInbox();
        assertTrue(inbox.offer("cmd-1", "single", "", "", 10_000L));
        assertNull(inbox.nextReady(1_000L, false));

        MiBandCommandInbox.Command command = inbox.nextReady(1_000L, true);
        assertEquals("cmd-1", command.id);
        assertEquals("single", command.pattern);
    }

    @Test public void duplicateIdExecutesOnlyOnceAfterSuccess() {
        MiBandCommandInbox inbox = new MiBandCommandInbox();
        assertTrue(inbox.offer("cmd-1", "call", "快看我", "星澜", 10_000L));
        assertFalse(inbox.offer("cmd-1", "call", "快看我", "星澜", 10_000L));
        MiBandCommandInbox.Command command = inbox.nextReady(1_000L, true);
        assertEquals("快看我", command.note);
        assertEquals("星澜", command.senderName);
        inbox.complete(command.id, true);

        assertNull(inbox.nextReady(1_001L, true));
        assertFalse(inbox.offer("cmd-1", "call", "快看我", "星澜", 10_000L));
    }

    @Test public void failedExecutionCanRetryButInFlightCannotDuplicate() {
        MiBandCommandInbox inbox = new MiBandCommandInbox();
        inbox.offer("cmd-1", "single", "纸条", "星澜", 10_000L);
        MiBandCommandInbox.Command first = inbox.nextReady(1_000L, true);
        assertNull(inbox.nextReady(1_000L, true));
        inbox.complete(first.id, false);
        assertEquals("cmd-1", inbox.nextReady(1_001L, true).id);
    }

    @Test public void expiredAndInvalidCommandsAreDiscarded() {
        MiBandCommandInbox inbox = new MiBandCommandInbox();
        assertFalse(inbox.offer("bad", "other", "纸条", "星澜", 10_000L));
        assertTrue(inbox.offer("expired", "single", "纸条", "星澜", 999L));
        assertNull(inbox.nextReady(1_000L, true));
        assertEquals(0, inbox.pendingCount());
    }
}
