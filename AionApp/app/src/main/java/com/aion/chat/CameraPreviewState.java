package com.aion.chat;

final class CameraPreviewState {
    private boolean running;
    private long lastFrameAt;
    private boolean appForeground;

    synchronized void started() {
        running = true;
        lastFrameAt = 0L;
    }

    synchronized void receivedFrame(long timestampMs) {
        if (running) lastFrameAt = timestampMs;
    }

    synchronized void failed() {
        running = false;
        lastFrameAt = 0L;
    }

    synchronized void stopped() {
        running = false;
        lastFrameAt = 0L;
    }

    synchronized boolean isRunning() {
        return running;
    }

    synchronized long getLastFrameAt() {
        return lastFrameAt;
    }

    synchronized void setAppForeground(boolean foreground) {
        appForeground = foreground;
    }

    synchronized boolean canStart() {
        return appForeground;
    }
}
