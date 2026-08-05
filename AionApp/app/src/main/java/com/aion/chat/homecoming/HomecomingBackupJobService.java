package com.aion.chat.homecoming;

import android.app.job.JobParameters;
import android.app.job.JobService;
import android.content.SharedPreferences;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class HomecomingBackupJobService extends JobService {
    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    @Override
    public boolean onStartJob(JobParameters parameters) {
        SharedPreferences preferences = getSharedPreferences(
                HomecomingModeStore.PREFERENCES_NAME, MODE_PRIVATE);
        String baseUrl = preferences.getString(
                HomecomingBackupScheduler.KEY_LAST_SERVER_BASE, "");
        if (baseUrl == null || baseUrl.isEmpty()
                || new HomecomingModeStore(this).isActive()) {
            return false;
        }
        executor.execute(() -> HomecomingBackupScheduler.createClient(this)
                .refreshSnapshot(
                        baseUrl,
                        HomecomingBackupClient.RefreshReason.PERIODIC_FALLBACK,
                        new HomecomingBackupClient.Callback() {
                            @Override
                            public void onSuccess(HomecomingReadiness readiness) {
                                preferences.edit()
                                        .putLong("last_checked_at", System.currentTimeMillis())
                                        .remove("last_error_code")
                                        .remove("last_error_message")
                                        .apply();
                                jobFinished(parameters, false);
                            }

                            @Override
                            public void onFailure(String code, String diagnostic) {
                                preferences.edit()
                                        .putString("last_error_code", code)
                                        .putLong("last_error_at", System.currentTimeMillis())
                                        .apply();
                                jobFinished(parameters, false);
                            }
                        }));
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters parameters) {
        return true;
    }

    @Override
    public void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }
}
