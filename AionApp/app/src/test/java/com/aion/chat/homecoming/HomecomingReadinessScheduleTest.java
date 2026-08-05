package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.Assert.assertEquals;

public class HomecomingReadinessScheduleTest {
    @Test
    public void exposesScheduleExactnessWithoutBlockingReadiness() throws Exception {
        Map<String, String> permissions = new LinkedHashMap<>();
        permissions.put("schedule_exactness", "permission_required");
        HomecomingReadiness readiness = new HomecomingReadiness(
                true, 1L, 2L, 3, 4, 5, 6, 7, 8, 9, 1,
                permissions, "");

        assertEquals("permission_required", readiness.scheduleExactness);
        assertEquals("permission_required",
                readiness.toJson().getString("scheduleExactness"));
        assertEquals(8, readiness.toJson().getInt("pendingScheduleCount"));
        assertEquals(true, readiness.toJson().getBoolean("ready"));
    }
}
