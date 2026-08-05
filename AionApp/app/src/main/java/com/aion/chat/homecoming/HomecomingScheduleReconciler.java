package com.aion.chat.homecoming;

import java.util.List;

public final class HomecomingScheduleReconciler {
    private static final long STALE_GRACE_MS = 90_000L;

    private final SchedulePort schedules;
    private final HomecomingScheduleCommandHandler.RegistrationPort registration;

    HomecomingScheduleReconciler(
            SchedulePort schedules,
            HomecomingScheduleCommandHandler.RegistrationPort registration) {
        if (schedules == null) throw new IllegalArgumentException("schedules are required");
        if (registration == null) {
            throw new IllegalArgumentException("registration is required");
        }
        this.schedules = schedules;
        this.registration = registration;
    }

    public HomecomingScheduleReconciler(
            HomecomingScheduleRepository repository,
            HomecomingScheduleCommandHandler.RegistrationPort registration) {
        this(new RepositoryAdapter(repository), registration);
    }

    public Result reconcile(long now) {
        int registered = 0;
        int missed = 0;
        int failed = 0;
        for (HomecomingScheduleRepository.Schedule schedule : schedules.listActive()) {
            if (schedule.triggerAt < now - STALE_GRACE_MS) {
                schedules.markMissed(schedule.id, now);
                try {
                    registration.cancel(schedule.id);
                } catch (RuntimeException ignored) {
                    // The database state is authoritative; a stale OS alarm cannot be trusted.
                }
                missed++;
                continue;
            }
            try {
                registration.register(schedule);
                registered++;
            } catch (RuntimeException exception) {
                failed++;
            }
        }
        return new Result(registered, missed, failed);
    }

    interface SchedulePort {
        List<HomecomingScheduleRepository.Schedule> listActive();
        void markMissed(String scheduleId, long now);
    }

    private static final class RepositoryAdapter implements SchedulePort {
        private final HomecomingScheduleRepository repository;
        RepositoryAdapter(HomecomingScheduleRepository repository) {
            if (repository == null) {
                throw new IllegalArgumentException("repository is required");
            }
            this.repository = repository;
        }
        @Override public List<HomecomingScheduleRepository.Schedule> listActive() {
            return repository.listActive();
        }
        @Override public void markMissed(String scheduleId, long now) {
            repository.markMissed(scheduleId, now);
        }
    }

    public static final class Result {
        public final int registered;
        public final int missed;
        public final int failed;
        Result(int registered, int missed, int failed) {
            this.registered = registered;
            this.missed = missed;
            this.failed = failed;
        }
    }
}
