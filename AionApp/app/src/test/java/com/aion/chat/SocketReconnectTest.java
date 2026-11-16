package com.aion.chat;

import org.junit.Test;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class SocketReconnectTest {
    private static class Clock implements SocketReconnect.Scheduler {
        Runnable task;
        long delay;
        public void post(Runnable task, long delay) { this.task = task; this.delay = delay; }
        public void remove(Runnable task) { if (this.task == task) this.task = null; }
        void run() { Runnable next = task; task = null; next.run(); }
    }

    @Test public void failuresCoalesceBackOffAndResetAfterConnecting() {
        Clock clock = new Clock(); List<String> attempts = new ArrayList<>();
        SocketReconnect retry = new SocketReconnect(clock, () -> attempts.add("connect"));
        for (long delay : new long[] {1000, 2000, 4000, 8000, 16000, 30000, 30000}) {
            retry.failed(); Runnable pending = clock.task;
            retry.failed(); assertSame(pending, clock.task); assertEquals(delay, clock.delay);
            clock.run();
        }
        retry.connected(); retry.failed(); assertEquals(1000, clock.delay);
        assertEquals(7, attempts.size());
    }

    @Test public void cancelledOrSupersededCallbacksCannotReconnectAnOldEndpoint() {
        Clock clock = new Clock(); List<String> attempts = new ArrayList<>();
        SocketReconnect retry = new SocketReconnect(clock, () -> attempts.add("connect"));
        retry.failed(); Runnable stale = clock.task;
        retry.soon(); stale.run(); assertTrue(attempts.isEmpty());
        assertEquals(500, clock.delay); clock.run(); assertEquals(1, attempts.size());
        retry.failed(); stale = clock.task; retry.cancel(); stale.run();
        assertEquals(1, attempts.size());
    }
}
