package com.aion.chat;

import java.lang.ref.WeakReference;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

final class PhoneCameraPreviewCoordinator {
    interface Client {
        void pauseForEvent(Runnable released);
        void resumeAfterEvent();
    }

    private static final PhoneCameraPreviewCoordinator SHARED =
            new PhoneCameraPreviewCoordinator();

    private final Object lock = new Object();
    private WeakReference<Client> clientRef = new WeakReference<>(null);
    private boolean eventActive;

    static PhoneCameraPreviewCoordinator shared() {
        return SHARED;
    }

    void register(Client client) {
        synchronized (lock) {
            clientRef = new WeakReference<>(client);
        }
    }

    void unregister(Client client) {
        synchronized (lock) {
            if (clientRef.get() == client) {
                clientRef.clear();
                clientRef = new WeakReference<>(null);
            }
        }
    }

    boolean pauseForEvent(long timeoutMs) {
        Client client;
        synchronized (lock) {
            eventActive = true;
            client = clientRef.get();
        }
        if (client == null) return true;

        CountDownLatch released = new CountDownLatch(1);
        client.pauseForEvent(released::countDown);
        try {
            return released.await(Math.max(0L, timeoutMs), TimeUnit.MILLISECONDS);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    void finishEvent() {
        Client client;
        synchronized (lock) {
            if (!eventActive) return;
            eventActive = false;
            client = clientRef.get();
        }
        if (client != null) client.resumeAfterEvent();
    }

    boolean isEventActive() {
        synchronized (lock) {
            return eventActive;
        }
    }
}
