package com.aion.chat.homecoming;

import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;

import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class HomecomingBackupScheduler {
    public static final int JOB_ID = 0x48434d31;
    private static final long DEBOUNCE_MS = 5 * 60 * 1000L;
    private static final long PERIODIC_MS = 15 * 60 * 1000L;
    static final String KEY_LAST_SERVER_BASE = "backup_server_base";
    static final String KEY_DEVICE_ID = "device_id";

    private final Clock clock;
    private final RequestSink requests;
    private final ModeState modeState;
    private final NetworkState networkState;
    private final AtomicBoolean inFlight = new AtomicBoolean(false);
    private long lastRequestAt = Long.MIN_VALUE;
    private String lastBaseUrl = "";

    HomecomingBackupScheduler(
            Clock clock,
            RequestSink requests,
            ModeState modeState,
            NetworkState networkState) {
        this.clock = clock;
        this.requests = requests;
        this.modeState = modeState;
        this.networkState = networkState;
    }

    public void onLauncherForeground(String serverBaseUrl) {
        String base = normalize(serverBaseUrl);
        if (base.isEmpty() || modeState.isActive()) {
            return;
        }
        lastBaseUrl = base;
        long now = clock.nowMs();
        if (lastRequestAt != Long.MIN_VALUE && now - lastRequestAt < DEBOUNCE_MS) {
            return;
        }
        request(base, HomecomingBackupClient.RefreshReason.FOREGROUND, now);
    }

    public void onNormalRouteSelected(String serverBaseUrl) {
        String base = normalize(serverBaseUrl);
        if (base.isEmpty() || modeState.isActive()) {
            return;
        }
        lastBaseUrl = base;
        request(base, HomecomingBackupClient.RefreshReason.ROUTE_SWITCH, clock.nowMs());
    }

    public void markDirty() {
        if (!lastBaseUrl.isEmpty() && !modeState.isActive()) {
            request(lastBaseUrl, HomecomingBackupClient.RefreshReason.CONFIG_EVENT, clock.nowMs());
        }
    }

    public void runPeriodicFallback() {
        if (!lastBaseUrl.isEmpty() && !modeState.isActive() && networkState.isAvailable()) {
            request(lastBaseUrl,
                    HomecomingBackupClient.RefreshReason.PERIODIC_FALLBACK,
                    clock.nowMs());
        }
    }

    private void request(
            String baseUrl, HomecomingBackupClient.RefreshReason reason, long now) {
        if (!inFlight.compareAndSet(false, true)) {
            return;
        }
        lastRequestAt = now;
        requests.request(baseUrl, reason, () -> inFlight.set(false));
    }

    public static HomecomingBackupScheduler create(Context context) {
        Context app = context.getApplicationContext();
        SharedPreferences preferences = app.getSharedPreferences(
                HomecomingModeStore.PREFERENCES_NAME, Context.MODE_PRIVATE);
        HomecomingModeStore modeStore = new HomecomingModeStore(app);
        HomecomingBackupClient client = createClient(app);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        RequestSink sink = (baseUrl, reason, completion) -> {
            preferences.edit().putString(KEY_LAST_SERVER_BASE, baseUrl).apply();
            executor.execute(() -> client.refreshSnapshot(baseUrl, reason,
                    new HomecomingBackupClient.Callback() {
                        @Override
                        public void onSuccess(HomecomingReadiness readiness) {
                            SharedPreferences.Editor editor = preferences.edit()
                                    .putLong("last_checked_at", System.currentTimeMillis())
                                    .remove("last_error_code")
                                    .remove("last_error_message");
                            if (readiness != null) {
                                editor.putString("readiness_json",
                                        readiness.toJson().toString());
                            }
                            editor.apply();
                            completion.run();
                        }

                        @Override
                        public void onFailure(String code, String diagnostic) {
                            preferences.edit()
                                    .putString("last_error_code", code)
                                    .putLong("last_error_at", System.currentTimeMillis())
                                    .putString("last_error_message", sanitize(diagnostic))
                                    .apply();
                            completion.run();
                        }
                    }));
        };
        NetworkState network = () -> {
            ConnectivityManager manager =
                    (ConnectivityManager) app.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (manager == null) {
                return false;
            }
            Network active = manager.getActiveNetwork();
            NetworkCapabilities capabilities =
                    active == null ? null : manager.getNetworkCapabilities(active);
            return capabilities != null
                    && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        };
        HomecomingBackupScheduler scheduler = new HomecomingBackupScheduler(
                System::currentTimeMillis, sink, modeStore::isActive, network);
        scheduler.lastBaseUrl = preferences.getString(KEY_LAST_SERVER_BASE, "");
        schedulePeriodic(app);
        return scheduler;
    }

    static HomecomingBackupClient createClient(Context context) {
        return new HomecomingBackupClient(
                new HomecomingSnapshotStore(context),
                new HomecomingBackupClient.OkHttpTransport(),
                new HomecomingKeyStore(context, getOrCreateDeviceId(context)),
                new HomecomingBackupClient.DatabaseSnapshotImporter(
                        new HomecomingDatabase(context)));
    }

    static String getOrCreateDeviceId(Context context) {
        SharedPreferences preferences = context.getSharedPreferences(
                HomecomingModeStore.PREFERENCES_NAME, Context.MODE_PRIVATE);
        String current = preferences.getString(KEY_DEVICE_ID, "");
        if (current != null && current.length() >= 8) {
            return current;
        }
        String created = "android:" + UUID.randomUUID();
        preferences.edit().putString(KEY_DEVICE_ID, created).apply();
        return created;
    }

    public static void schedulePeriodic(Context context) {
        JobScheduler scheduler =
                (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler == null) {
            return;
        }
        JobInfo job = new JobInfo.Builder(
                JOB_ID,
                new ComponentName(context, HomecomingBackupJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(false)
                .setPeriodic(PERIODIC_MS)
                .build();
        scheduler.schedule(job);
    }

    private static String normalize(String value) {
        return value == null ? "" : value.trim();
    }

    private static String sanitize(String diagnostic) {
        if (diagnostic == null) {
            return "";
        }
        String compact = diagnostic.replace('\n', ' ').replace('\r', ' ').trim();
        return compact.length() > 240 ? compact.substring(0, 240) : compact;
    }

    interface Clock {
        long nowMs();
    }

    interface RequestSink {
        void request(
                String baseUrl,
                HomecomingBackupClient.RefreshReason reason,
                Runnable completion);
    }

    interface ModeState {
        boolean isActive();
    }

    interface NetworkState {
        boolean isAvailable();
    }
}
