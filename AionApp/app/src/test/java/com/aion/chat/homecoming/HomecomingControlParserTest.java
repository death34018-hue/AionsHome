package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.Arrays;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class HomecomingControlParserTest {
    @Test
    public void stripsOnlyAllowlistedExistingControlTags() {
        HomecomingControlParser.Result result = HomecomingControlParser.parse(
                "好的[ALARM:2030-01-02T08:00|起床]"
                        + "[REMINDER:2030-01-02|带伞]"
                        + "[Monitor:2030-01-02T20:00|检查运动]"
                        + "[SCHEDULE_DEL:s-1][SCHEDULE_LIST][CAM_CHECK]"
                        + "[APP_LOCK:games|20|休息一下]");

        assertEquals("好的", result.visibleText);
        assertEquals(Arrays.asList(
                "alarm", "reminder", "monitor", "schedule_delete",
                "schedule_list", "camera_check", "app_supervision"), result.types());
        assertEquals(Arrays.asList("2030-01-02T08:00", "起床"),
                result.events.get(0).arguments);
        assertEquals(Arrays.asList("s-1"), result.events.get(3).arguments);
        assertTrue(result.events.get(4).arguments.isEmpty());
        assertEquals(Arrays.asList("games", "20", "休息一下"),
                result.events.get(6).arguments);
    }

    @Test
    public void unknownToolTagRemainsVisibleAndIsNeverSelected() {
        HomecomingControlParser.Result result =
                HomecomingControlParser.parse("不能执行 [PLAY_MUSIC:test]");
        assertEquals("不能执行 [PLAY_MUSIC:test]", result.visibleText);
        assertTrue(result.events.isEmpty());
    }

    @Test
    public void malformedKnownTagStaysVisibleInsteadOfExecuting() {
        HomecomingControlParser.Result result =
                HomecomingControlParser.parse("不能执行 [ALARM:2030-01-02T08:00]");
        assertEquals("不能执行 [ALARM:2030-01-02T08:00]", result.visibleText);
        assertTrue(result.events.isEmpty());
    }
}
