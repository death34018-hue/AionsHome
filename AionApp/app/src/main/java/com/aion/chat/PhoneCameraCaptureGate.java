package com.aion.chat;

final class PhoneCameraCaptureGate {
    private long generation;
    private boolean busy;

    synchronized long begin() {
        if (busy) return -1L;
        busy = true;
        return ++generation;
    }

    synchronized boolean isCurrent(long token) {
        return busy && token == generation;
    }

    synchronized boolean complete(long token) {
        if (!isCurrent(token)) return false;
        busy = false;
        return true;
    }

    synchronized void cancel() {
        generation++;
        busy = false;
    }
}
