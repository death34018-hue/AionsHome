package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;

public class HomecomingScheduleReconcilerTest {
    @Test
    public void registersFutureAndGracePeriodSchedulesButMarksOldOnesMissed() {
        long now = 1_000_000L;
        FakeSchedules schedules = new FakeSchedules(Arrays.asList(
                schedule("future", now + 60_000L),
                schedule("grace", now - 89_000L),
                schedule("old", now - 90_001L)));
        FakeRegistration registration = new FakeRegistration();
        HomecomingScheduleReconciler reconciler =
                new HomecomingScheduleReconciler(schedules, registration);

        HomecomingScheduleReconciler.Result result = reconciler.reconcile(now);

        assertEquals(Arrays.asList("future", "grace"), registration.registered);
        assertEquals(Arrays.asList("old"), schedules.missed);
        assertEquals(Arrays.asList("old"), registration.cancelled);
        assertEquals(2, result.registered);
        assertEquals(1, result.missed);
        assertEquals(0, result.failed);
    }

    @Test
    public void registrationFailureDoesNotDeleteSchedule() {
        long now = 1_000_000L;
        FakeSchedules schedules = new FakeSchedules(
                Arrays.asList(schedule("future", now + 60_000L)));
        FakeRegistration registration = new FakeRegistration();
        registration.fail = true;

        HomecomingScheduleReconciler.Result result =
                new HomecomingScheduleReconciler(
                        schedules, registration).reconcile(now);

        assertEquals(1, result.failed);
        assertEquals(0, schedules.missed.size());
        assertEquals(1, schedules.active.size());
    }

    private static HomecomingScheduleRepository.Schedule schedule(
            String id, long triggerAt) {
        return new HomecomingScheduleRepository.Schedule(
                id, "alarm", triggerAt, "起床", "main", "main_private",
                "", "active", 1L, 1L, null);
    }

    private static final class FakeSchedules
            implements HomecomingScheduleReconciler.SchedulePort {
        final List<HomecomingScheduleRepository.Schedule> active;
        final List<String> missed = new ArrayList<>();
        FakeSchedules(List<HomecomingScheduleRepository.Schedule> active) {
            this.active = new ArrayList<>(active);
        }
        @Override public List<HomecomingScheduleRepository.Schedule> listActive() {
            return new ArrayList<>(active);
        }
        @Override public void markMissed(String scheduleId, long now) {
            missed.add(scheduleId);
        }
    }

    private static final class FakeRegistration
            implements HomecomingScheduleCommandHandler.RegistrationPort {
        final List<String> registered = new ArrayList<>();
        final List<String> cancelled = new ArrayList<>();
        boolean fail;
        @Override public void register(HomecomingScheduleRepository.Schedule schedule) {
            if (fail) throw new IllegalStateException("registration unavailable");
            registered.add(schedule.id);
        }
        @Override public void cancel(String scheduleId) {
            cancelled.add(scheduleId);
        }
    }
}
