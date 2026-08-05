package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

public class HomecomingAlarmRegistrarTest {
    @Test
    public void stableRequestCodeSeparatesScheduleIds() {
        int first = HomecomingAlarmRegistrar.requestCode("schedule-one");
        assertEquals(first, HomecomingAlarmRegistrar.requestCode("schedule-one"));
        assertNotEquals(first, HomecomingAlarmRegistrar.requestCode("schedule-two"));
        assertTrue(first >= 0);
    }

    @Test
    public void exactPermissionUsesExactIdleAlarm() {
        FakePlatform platform = new FakePlatform();
        platform.permissionRequired = true;
        platform.canExact = true;
        HomecomingAlarmRegistrar registrar =
                new HomecomingAlarmRegistrar(platform, "epoch-one");

        registrar.register(schedule("schedule-one", 20_000L));

        assertEquals("exact", registrar.exactness());
        assertTrue(platform.scheduled.get(0).exact);
        assertEquals("epoch-one", platform.scheduled.get(0).epochId);
    }

    @Test
    public void missingExactPermissionFallsBackAndReportsRisk() {
        FakePlatform platform = new FakePlatform();
        platform.permissionRequired = true;
        platform.canExact = false;
        HomecomingAlarmRegistrar registrar =
                new HomecomingAlarmRegistrar(platform, "epoch-one");

        registrar.register(schedule("schedule-one", 20_000L));

        assertEquals("permission_required", registrar.exactness());
        assertFalse(platform.scheduled.get(0).exact);
    }

    @Test
    public void cancelUsesSameStableIdentity() {
        FakePlatform platform = new FakePlatform();
        HomecomingAlarmRegistrar registrar =
                new HomecomingAlarmRegistrar(platform, "epoch-one");
        registrar.register(schedule("schedule-one", 20_000L));

        registrar.cancel("schedule-one");

        assertEquals(platform.scheduled.get(0).requestCode,
                platform.cancelledRequestCode);
        assertEquals("schedule-one", platform.cancelledScheduleId);
    }

    private static HomecomingScheduleRepository.Schedule schedule(
            String id, long triggerAt) {
        return new HomecomingScheduleRepository.Schedule(
                id, "alarm", triggerAt, "起床", "main", "main_private",
                "", "active", 1L, 1L, null);
    }

    private static final class FakePlatform
            implements HomecomingAlarmRegistrar.Platform {
        final List<Registration> scheduled = new ArrayList<>();
        boolean permissionRequired;
        boolean canExact = true;
        int cancelledRequestCode = -1;
        String cancelledScheduleId;

        @Override public boolean exactPermissionRequired() {
            return permissionRequired;
        }
        @Override public boolean canScheduleExact() {
            return canExact;
        }
        @Override public void schedule(
                int requestCode, long triggerAt, boolean exact,
                String scheduleId, String epochId) {
            scheduled.add(new Registration(
                    requestCode, triggerAt, exact, scheduleId, epochId));
        }
        @Override public void cancel(
                int requestCode, String scheduleId, String epochId) {
            cancelledRequestCode = requestCode;
            cancelledScheduleId = scheduleId;
        }
    }

    private static final class Registration {
        final int requestCode;
        final long triggerAt;
        final boolean exact;
        final String scheduleId;
        final String epochId;
        Registration(
                int requestCode, long triggerAt, boolean exact,
                String scheduleId, String epochId) {
            this.requestCode = requestCode;
            this.triggerAt = triggerAt;
            this.exact = exact;
            this.scheduleId = scheduleId;
            this.epochId = epochId;
        }
    }
}
