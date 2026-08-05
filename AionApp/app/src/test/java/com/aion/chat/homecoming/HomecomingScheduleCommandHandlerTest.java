package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TimeZone;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

public class HomecomingScheduleCommandHandlerTest {
    private static final long NOW = 1_893_456_000_000L; // 2030-01-01T00:00:00Z

    @Test
    public void appliesCreateOncePerRequestAndIndexThenRegisters() {
        FakeSchedules schedules = new FakeSchedules();
        FakeRegistration registration = new FakeRegistration();
        HomecomingScheduleCommandHandler handler =
                handler(schedules, registration);
        HomecomingControlParser.ControlEvent event =
                HomecomingControlParser.parse(
                        "[ALARM:2030-01-02T08:00|起床]").events.get(0);

        HomecomingScheduleCommandHandler.ApplyResult first = handler.apply(
                "request-one", 0, event, "main", "main_private", NOW);
        HomecomingScheduleCommandHandler.ApplyResult duplicate = handler.apply(
                "request-one", 0, event, "main", "main_private", NOW + 1L);

        assertEquals("created", first.status);
        assertEquals("already_applied", duplicate.status);
        assertEquals(1, schedules.created.size());
        assertEquals(1, registration.registered.size());
        assertEquals(schedules.created.get(0).id, registration.registered.get(0).id);
    }

    @Test
    public void acceptsExistingDateShapesAndDateOnlyDefaultsToNine() {
        FakeSchedules schedules = new FakeSchedules();
        HomecomingScheduleCommandHandler handler =
                handler(schedules, new FakeRegistration());

        handler.apply("one", 0, parse("[REMINDER:2030-01-03|带伞]"),
                "second", "companion_private", NOW);
        handler.apply("two", 0, parse("[Monitor:2030/01/04 20:30|检查运动]"),
                "second", "group", NOW);
        handler.apply("three", 0, parse("[ALARM:01-05 07:15|喝水]"),
                "main", "main_private", NOW);

        assertEquals(3, schedules.created.size());
        assertEquals(9, localHour(schedules.created.get(0).triggerAt));
        assertEquals(20, localHour(schedules.created.get(1).triggerAt));
        assertEquals(7, localHour(schedules.created.get(2).triggerAt));
    }

    @Test
    public void deletesOnceAndCancelsRegistration() {
        FakeSchedules schedules = new FakeSchedules();
        HomecomingScheduleRepository.Schedule existing = schedule(
                "schedule-one", "alarm", NOW + 50_000L, "起床",
                "main", "main_private", "active");
        schedules.values.put(existing.id, existing);
        FakeRegistration registration = new FakeRegistration();
        HomecomingScheduleCommandHandler handler =
                handler(schedules, registration);

        HomecomingScheduleCommandHandler.ApplyResult first = handler.apply(
                "request-delete", 0, parse("[SCHEDULE_DEL:schedule-one]"),
                "main", "main_private", NOW);
        HomecomingScheduleCommandHandler.ApplyResult duplicate = handler.apply(
                "request-delete", 0, parse("[SCHEDULE_DEL:schedule-one]"),
                "main", "main_private", NOW);

        assertEquals("deleted", first.status);
        assertEquals("already_applied", duplicate.status);
        assertEquals(1, schedules.deleted.size());
        assertEquals(1, registration.cancelled.size());
    }

    @Test
    public void malformedStaleAndOversizedCommandsNeverCreate() {
        FakeSchedules schedules = new FakeSchedules();
        HomecomingScheduleCommandHandler handler =
                handler(schedules, new FakeRegistration());

        HomecomingScheduleCommandHandler.ApplyResult invalidDate = handler.apply(
                "bad-date", 0,
                parse("[ALARM:2030-02-30T08:00|x]"),
                "main", "main_private", NOW);
        HomecomingScheduleCommandHandler.ApplyResult stale = handler.apply(
                "stale", 0,
                parse("[ALARM:2029-01-01T08:00|x]"),
                "main", "main_private", NOW);
        StringBuilder huge = new StringBuilder();
        while (huge.length() <= 2_000) huge.append('x');
        HomecomingScheduleCommandHandler.ApplyResult oversized = handler.apply(
                "huge", 0,
                parse("[ALARM:2030-01-02T08:00|" + huge + "]"),
                "main", "main_private", NOW);

        assertEquals("invalid", invalidDate.status);
        assertEquals("invalid", stale.status);
        assertEquals("invalid", oversized.status);
        assertTrue(schedules.created.isEmpty());
    }

    @Test
    public void registrationFailureKeepsCreatedScheduleAndReportsDegradation() {
        FakeSchedules schedules = new FakeSchedules();
        FakeRegistration registration = new FakeRegistration();
        registration.fail = true;
        HomecomingScheduleCommandHandler handler =
                handler(schedules, registration);

        HomecomingScheduleCommandHandler.ApplyResult result = handler.apply(
                "request-one", 0,
                parse("[ALARM:2030-01-02T08:00|起床]"),
                "main", "main_private", NOW);

        assertEquals("registration_failed", result.status);
        assertEquals(1, schedules.created.size());
        assertNotNull(schedules.find(result.scheduleId));
    }

    @Test
    public void nonScheduleControlsRemainDeferred() {
        HomecomingScheduleCommandHandler handler =
                handler(new FakeSchedules(), new FakeRegistration());

        HomecomingScheduleCommandHandler.ApplyResult result = handler.apply(
                "camera", 0, parse("[CAM_CHECK]"),
                "main", "main_private", NOW);

        assertEquals("deferred", result.status);
        assertFalse(result.applied);
    }

    private static HomecomingScheduleCommandHandler handler(
            FakeSchedules schedules, FakeRegistration registration) {
        return new HomecomingScheduleCommandHandler(
                schedules, registration, TimeZone.getTimeZone("Asia/Shanghai"));
    }

    private static HomecomingControlParser.ControlEvent parse(String text) {
        return HomecomingControlParser.parse(text).events.get(0);
    }

    private static int localHour(long millis) {
        java.util.Calendar calendar = java.util.Calendar.getInstance(
                TimeZone.getTimeZone("Asia/Shanghai"));
        calendar.setTimeInMillis(millis);
        return calendar.get(java.util.Calendar.HOUR_OF_DAY);
    }

    private static HomecomingScheduleRepository.Schedule schedule(
            String id, String type, long triggerAt, String content,
            String ownerId, String timelineId, String status) {
        return new HomecomingScheduleRepository.Schedule(
                id, type, triggerAt, content, ownerId, timelineId,
                "", status, NOW, NOW, null);
    }

    private static final class FakeSchedules
            implements HomecomingScheduleCommandHandler.SchedulePort {
        final Map<String, HomecomingScheduleRepository.Schedule> values =
                new LinkedHashMap<>();
        final List<HomecomingScheduleRepository.Schedule> created = new ArrayList<>();
        final List<String> deleted = new ArrayList<>();

        @Override
        public HomecomingScheduleRepository.Schedule find(String id) {
            return values.get(id);
        }

        @Override
        public HomecomingScheduleRepository.Schedule createWithId(
                String id, String type, long triggerAt, String content,
                String ownerId, String timelineId, long now) {
            HomecomingScheduleRepository.Schedule existing = values.get(id);
            if (existing != null) return existing;
            HomecomingScheduleRepository.Schedule value = schedule(
                    id, type, triggerAt, content, ownerId, timelineId, "active");
            values.put(id, value);
            created.add(value);
            return value;
        }

        @Override
        public HomecomingScheduleRepository.Schedule delete(String id, long now) {
            HomecomingScheduleRepository.Schedule current = values.get(id);
            HomecomingScheduleRepository.Schedule deletedValue = schedule(
                    id, current.type, current.triggerAt, current.content,
                    current.ownerId, current.timelineId, "deleted");
            values.put(id, deletedValue);
            deleted.add(id);
            return deletedValue;
        }
    }

    private static final class FakeRegistration
            implements HomecomingScheduleCommandHandler.RegistrationPort {
        final List<HomecomingScheduleRepository.Schedule> registered =
                new ArrayList<>();
        final List<String> cancelled = new ArrayList<>();
        boolean fail;

        @Override
        public void register(HomecomingScheduleRepository.Schedule schedule) {
            if (fail) throw new IllegalStateException("alarm unavailable");
            registered.add(schedule);
        }

        @Override
        public void cancel(String scheduleId) {
            cancelled.add(scheduleId);
        }
    }
}
