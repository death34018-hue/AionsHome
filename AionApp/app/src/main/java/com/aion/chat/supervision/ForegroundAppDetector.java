package com.aion.chat.supervision;

import android.app.usage.UsageEvents;
import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.content.Context;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

public class ForegroundAppDetector {
    public interface Listener {
        void onForegroundPackage(String packageName);
    }

    private static final String TAG = "AionForeground";
    private static final long POLL_INTERVAL_MS = 2_000L;
    private static final long ONE_SHOT_RETRY_DELAY_MS = 2_000L;
    private final UsageStatsManager usageStatsManager;
    private final Handler mainHandler;
    private final ScheduledExecutorService worker;
    private final LatestRequestGate oneShotGate = new LatestRequestGate();
    private volatile Listener listener;
    private volatile boolean running;
    private ScheduledFuture<?> periodicFuture;
    private ScheduledFuture<?> oneShotFuture;
    private long cursorWallMs;

    protected ForegroundAppDetector() {
        usageStatsManager = null;
        mainHandler = null;
        worker = null;
    }

    public ForegroundAppDetector(Context context) {
        usageStatsManager = (UsageStatsManager) context.getApplicationContext()
                .getSystemService(Context.USAGE_STATS_SERVICE);
        mainHandler = new Handler(Looper.getMainLooper());
        worker = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "AionForegroundDetector");
            thread.setDaemon(true);
            return thread;
        });
        cursorWallMs = Math.max(0L, System.currentTimeMillis() - 10_000L);
    }

    public synchronized void start(Listener listener) {
        if (listener == null) {
            throw new IllegalArgumentException("listener is required");
        }
        this.listener = listener;
        if (running) return;
        running = true;
        if (worker != null) {
            periodicFuture = worker.scheduleWithFixedDelay(
                    this::pollPeriodicOnWorker,
                    0L,
                    POLL_INTERVAL_MS,
                    TimeUnit.MILLISECONDS);
        }
    }

    public synchronized void stop() {
        running = false;
        if (periodicFuture != null) {
            periodicFuture.cancel(false);
            periodicFuture = null;
        }
        cancelPendingOneShot();
    }

    public void pollNow() {
        Listener currentListener = listener;
        if (currentListener != null) pollOnce(currentListener);
    }

    public synchronized void pollOnce(Listener oneShotListener) {
        if (oneShotListener == null) return;
        cancelOneShotFutureLocked();
        long generation = oneShotGate.next();
        scheduleOneShotAttempt(oneShotListener, generation, 0L, true);
    }

    public synchronized void cancelPendingOneShot() {
        oneShotGate.cancel();
        cancelOneShotFutureLocked();
    }

    public static ForegroundAppDetector create(Context context) {
        return new ForegroundAppDetector(context);
    }

    String detectLatestForegroundPackage(long nowWallMs) {
        if (usageStatsManager == null) return null;
        long startWallMs = Math.min(cursorWallMs, nowWallMs);
        String latestPackage = null;
        long latestTimestamp = -1L;
        UsageEvents events = usageStatsManager.queryEvents(startWallMs, nowWallMs);
        if (events != null) {
            UsageEvents.Event event = new UsageEvents.Event();
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                int type = event.getEventType();
                boolean foreground = type == UsageEvents.Event.MOVE_TO_FOREGROUND;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    foreground |= type == UsageEvents.Event.ACTIVITY_RESUMED;
                }
                if (foreground && event.getTimeStamp() >= latestTimestamp) {
                    latestTimestamp = event.getTimeStamp();
                    latestPackage = event.getPackageName();
                }
            }
        }
        cursorWallMs = nowWallMs;
        if (latestPackage != null) return latestPackage;

        List<UsageStats> stats = usageStatsManager.queryUsageStats(
                UsageStatsManager.INTERVAL_DAILY,
                Math.max(0L, nowWallMs - 24L * 60L * 60L * 1000L),
                nowWallMs);
        if (stats == null || stats.isEmpty()) return null;
        return Collections.max(stats, Comparator.comparingLong(UsageStats::getLastTimeUsed))
                .getPackageName();
    }

    private void pollPeriodicOnWorker() {
        if (!running) return;
        String packageName = detectSafely();
        Listener currentListener = listener;
        if (packageName != null && currentListener != null && mainHandler != null) {
            mainHandler.post(() -> {
                if (running && listener == currentListener) {
                    currentListener.onForegroundPackage(packageName);
                }
            });
        }
    }

    private synchronized void scheduleOneShotAttempt(
            Listener oneShotListener,
            long generation,
            long delayMs,
            boolean retryAllowed) {
        if (worker == null || !oneShotGate.isCurrent(generation)) return;
        oneShotFuture = worker.schedule(
                () -> pollOneShotOnWorker(
                        oneShotListener, generation, retryAllowed),
                delayMs,
                TimeUnit.MILLISECONDS);
    }

    private void pollOneShotOnWorker(
            Listener oneShotListener,
            long generation,
            boolean retryAllowed) {
        if (!oneShotGate.isCurrent(generation)) return;
        String packageName = detectSafely();
        if (!oneShotGate.isCurrent(generation)) return;
        if (packageName == null) {
            if (retryAllowed) {
                scheduleOneShotAttempt(
                        oneShotListener,
                        generation,
                        ONE_SHOT_RETRY_DELAY_MS,
                        false);
            }
            return;
        }
        if (mainHandler != null) {
            mainHandler.post(() -> {
                if (oneShotGate.isCurrent(generation)) {
                    oneShotListener.onForegroundPackage(packageName);
                }
            });
        }
    }

    private String detectSafely() {
        try {
            return detectLatestForegroundPackage(System.currentTimeMillis());
        } catch (RuntimeException error) {
            Log.w(TAG, "foreground query failed: " + error.getMessage());
            return null;
        }
    }

    private void cancelOneShotFutureLocked() {
        if (oneShotFuture != null) {
            oneShotFuture.cancel(false);
            oneShotFuture = null;
        }
    }
}
