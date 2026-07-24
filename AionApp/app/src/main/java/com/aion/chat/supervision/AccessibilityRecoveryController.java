package com.aion.chat.supervision;

import android.Manifest;
import android.content.ComponentName;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;

import com.aion.chat.AionAccessibilityService;

import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.Set;

public class AccessibilityRecoveryController {
    public interface SecureSettingsWriter {
        boolean hasPermission();
        boolean enableServicePreservingOthers(String componentName);
    }

    public interface RetryScheduler {
        void schedule(Runnable runnable, long delayMs);
        void cancel();
    }

    private static final long[] RETRY_DELAYS_MS = {1_000L, 2_000L, 5_000L};

    private final SecureSettingsWriter writer;
    private final RetryScheduler scheduler;
    private final String componentName;
    private final Set<String> sensitivePackages;
    private RecoveryState state = RecoveryState.ACTIVE;
    private boolean screenReady = true;
    private int retryIndex;
    private String diagnostic = "";

    protected AccessibilityRecoveryController() {
        writer = null;
        scheduler = null;
        componentName = "";
        sensitivePackages = new LinkedHashSet<>();
    }

    public AccessibilityRecoveryController(Context context) {
        Context appContext = context.getApplicationContext();
        writer = new AndroidSecureSettingsWriter(appContext);
        scheduler = new HandlerRetryScheduler();
        componentName = new ComponentName(appContext, AionAccessibilityService.class)
                .flattenToString();
        sensitivePackages = new LinkedHashSet<>(Arrays.asList(
                "com.eg.android.AlipayGphone",
                "com.tencent.mm"));
    }

    AccessibilityRecoveryController(SecureSettingsWriter writer, RetryScheduler scheduler,
            String componentName, Set<String> sensitivePackages) {
        if (writer == null || scheduler == null || componentName == null
                || componentName.trim().isEmpty() || sensitivePackages == null) {
            throw new IllegalArgumentException("recovery collaborators are required");
        }
        this.writer = writer;
        this.scheduler = scheduler;
        this.componentName = componentName;
        this.sensitivePackages = new LinkedHashSet<>(sensitivePackages);
    }

    public void onAccessibilityConnected() {
        if (scheduler != null) scheduler.cancel();
        retryIndex = 0;
        diagnostic = "";
        state = RecoveryState.ACTIVE;
    }

    public void onAccessibilityUnavailable(boolean screenReady) {
        if (scheduler != null) scheduler.cancel();
        this.screenReady = screenReady;
        retryIndex = 0;
        diagnostic = "accessibility service suspended by system";
        state = RecoveryState.ROM_SUSPENDED;
    }

    public void onScreenOff() {
        screenReady = false;
        if (scheduler != null) scheduler.cancel();
        retryIndex = 0;
        if (state == RecoveryState.RECOVERING) {
            state = RecoveryState.ROM_SUSPENDED;
            diagnostic = "recovery paused while screen is off";
        }
    }

    public void onUserPresent() {
        screenReady = true;
    }

    public void onForegroundPackage(String packageName, boolean monitored) {
        if (!screenReady || state == RecoveryState.ACTIVE
                || state == RecoveryState.PERMISSION_MISSING) {
            return;
        }
        if (packageName == null || sensitivePackages.contains(packageName)) {
            if (scheduler != null) scheduler.cancel();
            state = RecoveryState.ROM_SUSPENDED;
            diagnostic = "recovery deferred for sensitive foreground";
            return;
        }
        if (monitored) {
            retryIndex = 0;
            attemptRestore();
        }
    }

    public RecoveryState getState() {
        return state;
    }

    public String getDiagnostic() {
        return diagnostic;
    }

    private void attemptRestore() {
        if (!screenReady || writer == null || scheduler == null) {
            return;
        }
        if (!writer.hasPermission()) {
            scheduler.cancel();
            state = RecoveryState.PERMISSION_MISSING;
            diagnostic = "WRITE_SECURE_SETTINGS permission missing";
            return;
        }
        writer.enableServicePreservingOthers(componentName);
        state = RecoveryState.RECOVERING;
        diagnostic = "waiting for accessibility connected callback";
        long delayMs = RETRY_DELAYS_MS[Math.min(retryIndex, RETRY_DELAYS_MS.length - 1)];
        retryIndex++;
        scheduler.schedule(this::attemptRestore, delayMs);
    }

    private static final class AndroidSecureSettingsWriter implements SecureSettingsWriter {
        private final Context context;

        AndroidSecureSettingsWriter(Context context) {
            this.context = context;
        }

        @Override
        public boolean hasPermission() {
            return context.checkCallingOrSelfPermission(
                    Manifest.permission.WRITE_SECURE_SETTINGS) == PackageManager.PERMISSION_GRANTED;
        }

        @Override
        public boolean enableServicePreservingOthers(String componentName) {
            try {
                String current = Settings.Secure.getString(
                        context.getContentResolver(),
                        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
                LinkedHashSet<String> components = new LinkedHashSet<>();
                if (current != null && !current.trim().isEmpty()) {
                    for (String component : current.split(":")) {
                        if (!component.trim().isEmpty()) components.add(component.trim());
                    }
                }
                components.add(componentName);
                StringBuilder joined = new StringBuilder();
                for (String component : components) {
                    if (joined.length() > 0) joined.append(':');
                    joined.append(component);
                }
                boolean servicesWritten = Settings.Secure.putString(
                        context.getContentResolver(),
                        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
                        joined.toString());
                boolean enabledWritten = Settings.Secure.putInt(
                        context.getContentResolver(),
                        Settings.Secure.ACCESSIBILITY_ENABLED,
                        1);
                return servicesWritten && enabledWritten;
            } catch (SecurityException exception) {
                return false;
            }
        }
    }

    private static final class HandlerRetryScheduler implements RetryScheduler {
        private final Handler handler = new Handler(Looper.getMainLooper());
        @Override public void schedule(Runnable runnable, long delayMs) {
            handler.removeCallbacksAndMessages(null);
            handler.postDelayed(runnable, delayMs);
        }
        @Override public void cancel() { handler.removeCallbacksAndMessages(null); }
    }
}
