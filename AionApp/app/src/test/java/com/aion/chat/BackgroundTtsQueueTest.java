package com.aion.chat;

import org.junit.Test;
import static org.junit.Assert.*;

public class BackgroundTtsQueueTest {
    @Test public void ordersSegmentsAndMessagesAndIgnoresDuplicates() {
        BackgroundTtsQueue queue = new BackgroundTtsQueue();
        queue.offer("checkpoint", 1, "/one");
        queue.offer("sentinel", 0, "/next");
        assertNull(queue.next());
        queue.offer("checkpoint", 0, "/zero");
        assertEquals("/zero", queue.next());
        queue.offer("checkpoint", 0, "/duplicate");
        queue.advance();
        assertEquals("/one", queue.next());
        queue.finish("checkpoint");
        queue.advance();
        assertEquals("/next", queue.next());
        queue.finish("sentinel");
        queue.advance();
        assertFalse(queue.hasPending());
        queue.offer("checkpoint", 0, "/late-copy");
        assertNull(queue.next());
    }

    @Test public void handoffSkipsSegmentsAlreadyPlayedByTheWebView() {
        BackgroundTtsQueue queue = new BackgroundTtsQueue();
        queue.offer("monitor", 2, "/remaining");
        assertNull(queue.next());
        queue.finish("monitor");
        assertEquals("/remaining", queue.next());
        queue.advance();
        assertNull(queue.next());
    }

    @Test public void clearStopsQueuedSpeech() {
        BackgroundTtsQueue queue = new BackgroundTtsQueue();
        queue.offer("monitor", 0, "/speech");
        queue.clear();
        assertFalse(queue.hasPending());
        assertNull(queue.next());
    }

    @Test public void lostConnectionDoesNotLeaveNextMessageWaitingForMissingDone() {
        BackgroundTtsQueue queue = new BackgroundTtsQueue();
        queue.offer("interrupted", 0, "/first");
        queue.advance();
        queue.finishPending();
        queue.offer("new-monitor", 0, "/next");
        assertEquals("/next", queue.next());
    }
}
