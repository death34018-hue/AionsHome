package com.aion.chat.supervision;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;

final class CheckpointDeadlineScheduler {
    private final AppSupervisionRuntime.Scheduler testScheduler;
    private final ScheduledExecutorService executor;
    private long generation;
    private ScheduledFuture<?> scheduledFuture;

    private CheckpointDeadlineScheduler(
            AppSupervisionRuntime.Scheduler testScheduler,
            ScheduledExecutorService executor) {
        this.testScheduler = testScheduler;
        this.executor = executor;
    }

    static CheckpointDeadlineScheduler production() {
        ThreadFactory factory = runnable -> {
            Thread thread = new Thread(runnable, "app-supervision-checkpoint");
            thread.setDaemon(true);
            return thread;
        };
        return new CheckpointDeadlineScheduler(
                null, Executors.newSingleThreadScheduledExecutor(factory));
    }

    static CheckpointDeadlineScheduler forTests(AppSupervisionRuntime.Scheduler scheduler) {
        return new CheckpointDeadlineScheduler(scheduler, null);
    }

    synchronized void replace(Runnable runnable, long delayMs) {
        cancelScheduledFuture();
        long expectedGeneration = ++generation;
        Runnable guarded = () -> {
            synchronized (CheckpointDeadlineScheduler.this) {
                if (expectedGeneration != generation) return;
                scheduledFuture = null;
            }
            runnable.run();
        };
        if (testScheduler != null) {
            testScheduler.schedule(guarded, Math.max(0L, delayMs));
        } else {
            scheduledFuture = executor.schedule(
                    guarded, Math.max(0L, delayMs), TimeUnit.MILLISECONDS);
        }
    }

    synchronized void cancel() {
        generation++;
        cancelScheduledFuture();
    }

    synchronized void shutdown() {
        cancel();
        if (executor != null) executor.shutdownNow();
    }

    private void cancelScheduledFuture() {
        if (scheduledFuture != null) {
            scheduledFuture.cancel(false);
            scheduledFuture = null;
        }
    }
}
