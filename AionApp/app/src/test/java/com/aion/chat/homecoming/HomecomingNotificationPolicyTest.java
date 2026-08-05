package com.aion.chat.homecoming;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingNotificationPolicyTest {
    @Test
    public void usesConfiguredNameAndHighPriorityForExistingScheduleTypes() {
        HomecomingNotificationController.Projection projection =
                HomecomingNotificationController.projection(
                        schedule("alarm"), "配置中的伴侣名", "该起床了");

        assertEquals("配置中的伴侣名", projection.title);
        assertEquals("该起床了", projection.text);
        assertTrue(projection.highPriority);
        assertTrue(projection.audible);
    }

    @Test
    public void persistentWorkNotificationIsLowNoise() {
        HomecomingNotificationController.Projection projection =
                HomecomingNotificationController.backgroundProjection();

        assertFalse(projection.highPriority);
        assertFalse(projection.audible);
        assertTrue(projection.ongoing);
    }

    @Test
    public void emptyConfiguredNameIsRejectedInsteadOfHardcoded() {
        org.junit.Assert.assertThrows(IllegalArgumentException.class, () ->
                HomecomingNotificationController.projection(
                        schedule("reminder"), "", "带伞"));
    }

    private static HomecomingScheduleRepository.Schedule schedule(String type) {
        return new HomecomingScheduleRepository.Schedule(
                "schedule-one", type, 10L, "内容", "main", "main_private",
                "", "active", 1L, 1L, null);
    }
}
