package com.aion.chat.homecoming;

import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

import com.aion.chat.supervision.AppSupervisionRuntime;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class HomecomingForegroundService extends Service {
    public static final String ACTION_START =
            "com.aion.chat.homecoming.START";
    public static final String ACTION_RECONCILE =
            "com.aion.chat.homecoming.RECONCILE";
    public static final String ACTION_FIRE =
            "com.aion.chat.homecoming.FIRE_SCHEDULE";
    private static final int NOTIFICATION_ID = 0x48434d32;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private HomecomingSupervisionController supervisionController;
    private HomecomingRuntime runtime;

    @Override
    public void onCreate() {
        super.onCreate();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        HomecomingModeStore mode = new HomecomingModeStore(this);
        if (!mode.isActive()) {
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        startForeground(
                NOTIFICATION_ID,
                new HomecomingNotificationController(this).backgroundNotification());
        ensureSupervisionController(mode);
        String action = intent == null ? "" : intent.getAction();
        executor.execute(() -> {
            if (ACTION_START.equals(action) || ACTION_RECONCILE.equals(action)
                    || action.isEmpty()) {
                try {
                    reconcileSchedules(mode);
                } catch (RuntimeException ignored) {
                }
                return;
            }
            if (ACTION_FIRE.equals(action) && validateFireIntent(mode, intent)) {
                try {
                    HomecomingRuntime runtime = new HomecomingRuntime(this, mode);
                    runtime.fireSchedule(
                            intent.getStringExtra(
                                    HomecomingAlarmRegistrar.EXTRA_SCHEDULE_ID),
                            intent.getLongExtra(
                                    HomecomingAlarmRegistrar.EXTRA_TRIGGER_AT, 0L),
                            new HomecomingTriggerEngine.Completion() {
                                @Override public void onComplete(
                                        String messageId, String text) {
                                }
                                @Override public void onFailure(String code) {
                                }
                                @Override public void onDuplicate() {
                                }
                            });
                    return;
                } catch (Exception ignored) {
                }
            }
        });
        return START_STICKY;
    }

    private void ensureSupervisionController(HomecomingModeStore mode) {
        if (supervisionController != null) return;
        AppSupervisionRuntime phoneRuntime =
                AppSupervisionRuntime.start(getApplicationContext());
        HomecomingSupervisionRepository events =
                new HomecomingSupervisionRepository(
                        new HomecomingDatabase(this),
                        mode.currentEpoch(),
                        HomecomingBackupScheduler.getOrCreateDeviceId(this));
        supervisionController = new HomecomingSupervisionController(
                mode::isActive,
                new HomecomingSupervisionController.RuntimePort() {
                    @Override public void setListener(
                            HomecomingSupervisionController.StateListener listener) {
                        phoneRuntime.setSyncListener(
                                listener == null
                                        ? null
                                        : listener::onStateEvent);
                    }

                    @Override public org.json.JSONObject buildStatePayload(
                            String eventType,
                            String groupId,
                            long checkpointMs) throws Exception {
                        return phoneRuntime.buildStatePayload(
                                eventType, groupId, checkpointMs);
                    }
                },
                new HomecomingSupervisionController.EventPort() {
                    @Override public HomecomingSupervisionRepository.Event enqueue(
                            String eventId,
                            String groupId,
                            long checkpointMs,
                            String roleId,
                            String payloadJson,
                            long now) {
                        return events.enqueue(
                                eventId,
                                groupId,
                                checkpointMs,
                                roleId,
                                payloadJson,
                                now);
                    }

                    @Override public java.util.List<
                            HomecomingSupervisionRepository.Event> recoverable(long now) {
                        return events.recoverable(now);
                    }
                },
                eventId -> executor.execute(() -> fireSupervisionEvent(mode, eventId)),
                System::currentTimeMillis);
        supervisionController.start();
    }

    private void fireSupervisionEvent(HomecomingModeStore mode, String eventId) {
        if (!mode.isActive()) return;
        try {
            if (runtime == null) runtime = new HomecomingRuntime(this, mode);
            runtime.fireSupervisionEvent(
                    eventId,
                    new HomecomingSupervisionTriggerEngine.Completion() {
                        @Override public void onComplete(String messageId, String text) {
                        }

                        @Override public void onFailure(String code) {
                        }

                        @Override public void onDuplicate() {
                        }
                    });
        } catch (Exception ignored) {
            // The persisted event remains recoverable for the next service start.
        }
    }

    private void reconcileSchedules(HomecomingModeStore mode) {
        HomecomingScheduleRepository schedules =
                new HomecomingScheduleRepository(
                        new HomecomingDatabase(this),
                        mode.currentEpoch(),
                        HomecomingBackupScheduler.getOrCreateDeviceId(this));
        HomecomingAlarmRegistrar alarms =
                new HomecomingAlarmRegistrar(this, mode.currentEpoch());
        new HomecomingScheduleReconciler(
                schedules, alarms).reconcile(System.currentTimeMillis());
    }

    private boolean validateFireIntent(HomecomingModeStore mode, Intent intent) {
        if (intent == null) return false;
        String epochId = intent.getStringExtra(
                HomecomingAlarmRegistrar.EXTRA_EPOCH_ID);
        if (epochId == null || !epochId.equals(mode.currentEpoch())) return false;
        String scheduleId = intent.getStringExtra(
                HomecomingAlarmRegistrar.EXTRA_SCHEDULE_ID);
        long triggerAt = intent.getLongExtra(
                HomecomingAlarmRegistrar.EXTRA_TRIGGER_AT, 0L);
        if (scheduleId == null || triggerAt <= 0L) return false;
        HomecomingScheduleRepository schedules = new HomecomingScheduleRepository(
                new HomecomingDatabase(this),
                mode.currentEpoch(),
                HomecomingBackupScheduler.getOrCreateDeviceId(this));
        HomecomingScheduleRepository.Schedule schedule = schedules.find(scheduleId);
        return schedule != null
                && "active".equals(schedule.status)
                && schedule.triggerAt == triggerAt;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        if (supervisionController != null) {
            supervisionController.stop();
            supervisionController = null;
        }
        runtime = null;
        executor.shutdownNow();
        super.onDestroy();
    }

}
