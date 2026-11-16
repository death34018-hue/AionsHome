package com.aion.chat;

/** One cancellable retry, independent of the slow background heartbeat. */
final class SocketReconnect {
    interface Scheduler {
        void post(Runnable task, long delayMs);
        void remove(Runnable task);
    }

    private final Scheduler scheduler;
    private final Runnable connect;
    private Runnable pending;
    private long nextDelay = 1000;

    SocketReconnect(Scheduler scheduler, Runnable connect) {
        this.scheduler = scheduler;
        this.connect = connect;
    }

    synchronized void failed() {
        if (pending != null) return;
        schedule(nextDelay);
        nextDelay = Math.min(nextDelay * 2, 30000);
    }

    synchronized void soon() {
        cancel();
        nextDelay = 1000;
        schedule(500);
    }

    synchronized void connected() {
        cancel();
        nextDelay = 1000;
    }

    synchronized void cancel() {
        if (pending != null) scheduler.remove(pending);
        pending = null;
    }

    private void schedule(long delay) {
        pending = new Runnable() {
            @Override public void run() {
                synchronized (SocketReconnect.this) {
                    if (pending != this) return;
                    pending = null;
                }
                connect.run();
            }
        };
        scheduler.post(pending, delay);
    }
}
