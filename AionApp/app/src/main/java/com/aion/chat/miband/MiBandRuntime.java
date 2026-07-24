package com.aion.chat.miband;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import java.util.Calendar;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class MiBandRuntime implements MiBandGattSession.Listener {
    public interface Listener {
        void onStatus(MiBandStatus status);
        void onSamples(List<MiBandProtocol.ActivitySample> samples);
    }

    public interface SampleSink {
        void upload(String deviceName, List<MiBandProtocol.ActivitySample> samples) throws Exception;
    }

    public interface Completion { void onComplete(boolean success); }

    private interface Operation { void run() throws Exception; }

    private static volatile MiBandRuntime instance;

    public static MiBandRuntime get(Context context) {
        if (instance == null) {
            synchronized (MiBandRuntime.class) {
                if (instance == null) instance = new MiBandRuntime(context.getApplicationContext());
            }
        }
        return instance;
    }

    private final SharedPreferences preferences;
    private final ExecutorService operationExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread thread = new Thread(r, "AionMiBandBle");
        thread.setDaemon(false);
        return thread;
    });
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();
    private final MiBandGattSession session;
    private final AtomicBoolean connectionQueued = new AtomicBoolean(false);
    private volatile SampleSink sampleSink;
    private volatile boolean manualDisconnect;
    private volatile int reconnectFailures;
    private volatile String state = "idle";
    private volatile String error = "";
    private volatile boolean syncing;
    private volatile int battery = -1;
    private volatile int latestHeartRate;
    private volatile long latestHeartRateAt;
    private volatile long lastSyncAt;
    private volatile long nextSyncAt;
    private volatile List<MiBandProtocol.ActivitySample> lastSamples = Collections.emptyList();
    private final Runnable reconnectRunnable = this::autoConnect;

    private MiBandRuntime(Context context) {
        preferences = context.getSharedPreferences(MiBandPreferences.PREFS_NAME, Context.MODE_PRIVATE);
        session = new MiBandGattSession(context, this);
        reconnectFailures = preferences.getInt(MiBandPreferences.KEY_RECONNECT_FAILURES, 0);
    }

    public void addListener(Listener listener) {
        if (listener == null) return;
        listeners.addIfAbsent(listener);
        listener.onStatus(status());
        if (!lastSamples.isEmpty()) listener.onSamples(lastSamples);
    }

    public void removeListener(Listener listener) { listeners.remove(listener); }
    public void setSampleSink(SampleSink sink) { sampleSink = sink; }

    public void saveConfig(String address, String name, String keyHex,
                           MiBandPreferences.Settings settings) {
        String normalizedAddress = address == null ? "" : address.trim().toUpperCase(Locale.US);
        if (!normalizedAddress.matches("(?i)^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")) {
            throw new IllegalArgumentException("蓝牙地址格式不正确");
        }
        MiBandPreferences.decodeAuthKey(keyHex);
        preferences.edit()
                .putString(MiBandPreferences.KEY_ADDRESS, normalizedAddress)
                .putString(MiBandPreferences.KEY_NAME, name == null ? "" : name.trim())
                .putString(MiBandPreferences.KEY_AUTH, keyHex.trim().toLowerCase(Locale.US))
                .putInt(MiBandPreferences.KEY_DAY_INTERVAL, settings.dayIntervalMinutes)
                .putInt(MiBandPreferences.KEY_NIGHT_START, settings.nightStartMinute)
                .putInt(MiBandPreferences.KEY_NIGHT_END, settings.nightEndMinute)
                .putInt(MiBandPreferences.KEY_NIGHT_INTERVAL, settings.nightIntervalMinutes)
                .apply();
        publish();
    }

    public void saveSelectedDevice(String address, String name, String keyHex,
                                   MiBandPreferences.Settings settings) {
        String effectiveKey = keyHex == null ? "" : keyHex.trim();
        if (effectiveKey.isEmpty()) {
            effectiveKey = preferences.getString(MiBandPreferences.KEY_AUTH, "");
        }
        saveConfig(address, name, effectiveKey, settings);
    }

    public MiBandPreferences.Settings settings() {
        MiBandPreferences.Settings defaults = MiBandPreferences.Settings.defaults();
        try {
            return new MiBandPreferences.Settings(
                    preferences.getInt(MiBandPreferences.KEY_DAY_INTERVAL, defaults.dayIntervalMinutes),
                    preferences.getInt(MiBandPreferences.KEY_NIGHT_START, defaults.nightStartMinute),
                    preferences.getInt(MiBandPreferences.KEY_NIGHT_END, defaults.nightEndMinute),
                    preferences.getInt(MiBandPreferences.KEY_NIGHT_INTERVAL, defaults.nightIntervalMinutes));
        } catch (IllegalArgumentException ignored) {
            return defaults;
        }
    }

    public void updateSchedule(MiBandPreferences.Settings settings) {
        preferences.edit()
                .putInt(MiBandPreferences.KEY_DAY_INTERVAL, settings.dayIntervalMinutes)
                .putInt(MiBandPreferences.KEY_NIGHT_START, settings.nightStartMinute)
                .putInt(MiBandPreferences.KEY_NIGHT_END, settings.nightEndMinute)
                .putInt(MiBandPreferences.KEY_NIGHT_INTERVAL, settings.nightIntervalMinutes)
                .apply();
        updateNextSyncAt();
        publish();
    }

    public String savedDeviceName() {
        return preferences.getString(MiBandPreferences.KEY_NAME, "");
    }

    public String savedDeviceMaskedAddress() {
        return MiBandPreferences.maskAddress(preferences.getString(MiBandPreferences.KEY_ADDRESS, ""));
    }

    public boolean authKeyConfigured() {
        String key = preferences.getString(MiBandPreferences.KEY_AUTH, "");
        return key != null && key.matches("(?i)^[0-9a-f]{32}$");
    }

    public void clearSavedDevice() {
        preferences.edit()
                .remove(MiBandPreferences.KEY_ADDRESS)
                .remove(MiBandPreferences.KEY_NAME)
                .remove(MiBandPreferences.KEY_RECONNECT_FAILURES)
                .remove(MiBandPreferences.KEY_NEXT_RECONNECT_AT)
                .apply();
        disconnect();
        publish();
    }

    public boolean hasConfig() {
        String address = preferences.getString(MiBandPreferences.KEY_ADDRESS, "");
        String key = preferences.getString(MiBandPreferences.KEY_AUTH, "");
        return address != null && !address.isEmpty() && key != null && key.matches("(?i)^[0-9a-f]{32}$");
    }

    public void connectSaved() {
        manualDisconnect = false;
        cancelReconnect(true);
        queueConnect();
    }

    public void autoConnect() {
        if (manualDisconnect || !hasConfig() || session.isAuthenticated()) return;
        long now = System.currentTimeMillis();
        long nextAllowed = preferences.getLong(MiBandPreferences.KEY_NEXT_RECONNECT_AT, 0L);
        if (nextAllowed > now) {
            state = "waiting_reconnect";
            mainHandler.removeCallbacks(reconnectRunnable);
            mainHandler.postDelayed(reconnectRunnable, Math.max(1_000L, nextAllowed - now));
            publish();
            return;
        }
        queueConnect();
    }

    private void queueConnect() {
        if (session.isAuthenticated() || !connectionQueued.compareAndSet(false, true)) return;
        state = "connecting";
        error = "";
        publish();
        operationExecutor.execute(() -> {
          try {
            String address = preferences.getString(MiBandPreferences.KEY_ADDRESS, "");
            String key = preferences.getString(MiBandPreferences.KEY_AUTH, "");
            if (address == null || address.isEmpty() || key == null || key.isEmpty()) {
                throw new IllegalStateException("请先在高级设置填写手环地址和密钥");
            }
            session.connect(address, MiBandPreferences.decodeAuthKey(key), 18_000L);
            battery = session.readBattery();
            reconnectFailures = 0;
            preferences.edit()
                    .putInt(MiBandPreferences.KEY_RECONNECT_FAILURES, 0)
                    .putLong(MiBandPreferences.KEY_NEXT_RECONNECT_AT, 0L)
                    .apply();
            state = "ready";
            error = "";
            updateNextSyncAt();
            publish();
          } catch (Exception failure) {
            state = session.isConnected() ? "error" : "disconnected";
            error = safeMessage(failure);
            publish();
            if (!manualDisconnect && hasConfig()) scheduleReconnect();
          } finally {
            connectionQueued.set(false);
          }
        });
    }

    public void manualReconnect() {
        manualDisconnect = false;
        cancelReconnect(true);
        operationExecutor.execute(() -> {
            session.close();
            mainHandler.post(this::connectSaved);
        });
    }

    public void disconnect() {
        manualDisconnect = true;
        cancelReconnect(false);
        operationExecutor.execute(() -> {
            session.close();
            state = "disconnected";
            error = "";
            publish();
        });
    }

    public void syncNow() {
        execute("syncing", () -> {
            if (session.isRealtime()) throw new IllegalStateException("请先关闭实时心率，再同步历史数据");
            syncing = true;
            publish();
            long now = System.currentTimeMillis();
            long cursor = preferences.getLong(MiBandPreferences.KEY_ACTIVITY_CURSOR,
                    now - 7L * 24L * 60L * 60L * 1000L);
            boolean fullActivitySynced = preferences.getBoolean(
                    MiBandPreferences.KEY_FULL_ACTIVITY_SYNCED, false);
            long syncStart = MiBandHistoryParser.syncStartMillis(now, cursor, fullActivitySynced);
            Calendar since = Calendar.getInstance();
            since.setTimeInMillis(syncStart);
            List<MiBandProtocol.ActivitySample> activity = session.fetchActivity(since);
            SampleSink sink = sampleSink;
            if (sink != null && !activity.isEmpty()) {
                sink.upload(deviceName(), activity);
            }
            long nextCursor = MiBandHistoryParser.nextCursor(syncStart, activity);
            preferences.edit()
                    .putLong(MiBandPreferences.KEY_ACTIVITY_CURSOR, nextCursor)
                    .putBoolean(MiBandPreferences.KEY_FULL_ACTIVITY_SYNCED, true)
                    .apply();
            lastSamples = activity;
            lastSyncAt = System.currentTimeMillis();
            state = "ready";
            error = "";
            syncing = false;
            updateNextSyncAt();
            publishSamples(activity);
            publish();
        }, true);
    }

    public void startRealtime() {
        execute("starting_realtime", () -> {
            if (syncing) throw new IllegalStateException("数据同步中，请稍后再开实时心率");
            session.startRealtime();
            state = "realtime";
            error = "";
            publish();
        }, true);
    }

    public void stopRealtime() {
        execute("stopping_realtime", () -> {
            session.stopRealtime();
            state = session.isConnected() ? "ready" : "disconnected";
            error = "";
            publish();
        }, false);
    }

    public void vibrate(String pattern) {
        vibrate(pattern, null);
    }

    public void vibrate(String pattern, Completion completion) {
        execute("vibrating", () -> {
            session.vibrate(pattern);
            state = session.isRealtime() ? "realtime" : "ready";
            error = "";
            publish();
        }, true, completion);
    }

    public void sendNote(String pattern, String senderName, String note, Completion completion) {
        execute("sending_note", () -> {
            session.sendNote(pattern, senderName, note);
            state = session.isRealtime() ? "realtime" : "ready";
            error = "";
            publish();
        }, true, completion);
    }

    public MiBandStatus status() {
        return new MiBandStatus(
                state,
                deviceName(),
                hasConfig(),
                session.isConnected(),
                session.isAuthenticated(),
                session.isRealtime(),
                syncing,
                battery,
                latestHeartRate,
                latestHeartRateAt,
                lastSyncAt,
                nextSyncAt,
                error);
    }

    public void shutdown() {
        manualDisconnect = true;
        cancelReconnect(false);
        session.close();
        operationExecutor.shutdownNow();
    }

    @Override public void onDisconnected(String reason) {
        state = "disconnected";
        error = reason == null ? "" : reason;
        publish();
        if (!manualDisconnect && !connectionQueued.get()) scheduleReconnect();
    }

    @Override public void onHeartRate(int bpm, long measuredAtMillis) {
        latestHeartRate = bpm;
        latestHeartRateAt = measuredAtMillis;
        MiBandProtocol.ActivitySample raw = new MiBandProtocol.ActivitySample(
                measuredAtMillis, 0, 0, 0, bpm, 0, 0, 0, 0, null);
        List<MiBandProtocol.ActivitySample> samples = Collections.singletonList(raw);
        lastSamples = samples;
        operationExecutor.execute(() -> {
            try {
                SampleSink sink = sampleSink;
                if (sink != null) sink.upload(deviceName(), samples);
                publishSamples(samples);
                publish();
            } catch (Exception uploadError) {
                error = "实时心率上传失败: " + safeMessage(uploadError);
                publish();
            }
        });
    }

    private void execute(String stage, Operation operation, boolean reconnectOnFailure) {
        execute(stage, operation, reconnectOnFailure, null);
    }

    private void execute(String stage, Operation operation, boolean reconnectOnFailure,
                         Completion completion) {
        state = stage;
        error = "";
        publish();
        operationExecutor.execute(() -> {
            try {
                operation.run();
                if (completion != null) completion.onComplete(true);
            } catch (Exception failure) {
                syncing = false;
                state = session.isConnected() ? "error" : "disconnected";
                error = safeMessage(failure);
                publish();
                if (completion != null) completion.onComplete(false);
                if (reconnectOnFailure && !manualDisconnect && hasConfig() && !session.isConnected()) {
                    scheduleReconnect();
                }
            }
        });
    }

    private void scheduleReconnect() {
        mainHandler.removeCallbacks(reconnectRunnable);
        reconnectFailures++;
        long delay = MiBandSyncSchedule.reconnectDelayMillis(reconnectFailures - 1);
        long nextAllowed = System.currentTimeMillis() + delay;
        preferences.edit()
                .putInt(MiBandPreferences.KEY_RECONNECT_FAILURES, reconnectFailures)
                .putLong(MiBandPreferences.KEY_NEXT_RECONNECT_AT, nextAllowed)
                .apply();
        state = "waiting_reconnect";
        mainHandler.postDelayed(reconnectRunnable, delay);
        publish();
    }

    private void cancelReconnect(boolean clearDeadline) {
        mainHandler.removeCallbacks(reconnectRunnable);
        if (clearDeadline) {
            preferences.edit().putLong(MiBandPreferences.KEY_NEXT_RECONNECT_AT, 0L).apply();
        }
    }

    private void updateNextSyncAt() {
        long delay = MiBandSyncSchedule.nextDelayMillis(Calendar.getInstance(), settings());
        nextSyncAt = delay < 0 ? 0L : System.currentTimeMillis() + delay;
    }

    private String deviceName() {
        String value = preferences.getString(MiBandPreferences.KEY_NAME, "");
        return value == null || value.isEmpty() ? "Xiaomi Smart Band 7" : value;
    }

    private void publish() {
        MiBandStatus snapshot = status();
        mainHandler.post(() -> {
            for (Listener listener : listeners) listener.onStatus(snapshot);
        });
    }

    private void publishSamples(List<MiBandProtocol.ActivitySample> samples) {
        List<MiBandProtocol.ActivitySample> snapshot = new java.util.ArrayList<>(samples);
        mainHandler.post(() -> {
            for (Listener listener : listeners) listener.onSamples(snapshot);
        });
    }

    private static String safeMessage(Exception error) {
        String message = error.getMessage();
        return message == null || message.trim().isEmpty() ? error.getClass().getSimpleName() : message;
    }
}
