package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class HomecomingControlDispatcherTest {
    @Test
    public void mixedReplyExecutesScheduleAndSupervisionWithoutDeferringEither() {
        RecordingSchedule schedules = new RecordingSchedule();
        RecordingSupervision supervision = new RecordingSupervision();
        RecordingResults results = new RecordingResults();
        HomecomingControlDispatcher dispatcher = new HomecomingControlDispatcher(
                schedules, supervision, results,
                (timeline, owner) -> "配置伴侣名");
        List<HomecomingControlParser.ControlEvent> controls =
                HomecomingControlParser.parse(
                        "[ALARM:2030-01-02T08:00|起床]"
                                + "[APP_LOCK:group-video|20|先休息]").events;

        dispatcher.apply(command(), controls, 1_000L);

        assertEquals(1, schedules.types.size());
        assertEquals("alarm", schedules.types.get(0));
        assertEquals(1, supervision.types.size());
        assertEquals("app_supervision", supervision.types.get(0));
        assertTrue(results.deferred.isEmpty());
        assertEquals(1, results.systemTexts.size());
        assertTrue(results.systemTexts.get(0).contains("配置伴侣名"));
        assertTrue(results.systemTexts.get(0).contains("短视频"));
        assertTrue(results.systemTexts.get(0).contains("20 分钟"));
    }

    @Test
    public void rejectedCommandCreatesTruthfulConfiguredFailureResult() {
        RecordingResults results = new RecordingResults();
        HomecomingControlDispatcher dispatcher = new HomecomingControlDispatcher(
                new RecordingSchedule(),
                (requestId, index, event, ownerId, now) ->
                        new HomecomingSupervisionCommandHandler.ApplyResult(
                                "feature_disabled", "", "lock",
                                20, "短视频", false),
                results,
                (timeline, owner) -> "配置伴侣名");

        dispatcher.apply(
                command(),
                HomecomingControlParser.parse(
                        "[APP_LOCK:group-video|20|先休息]").events,
                1_000L);

        assertEquals(1, results.systemTexts.size());
        assertTrue(results.systemTexts.get(0).contains("未执行"));
        assertTrue(results.systemTexts.get(0).contains("监督功能未启用"));
        assertTrue(results.systemTexts.get(0).contains("配置伴侣名"));
    }

    @Test
    public void backgroundMonitorUsesTheSameMixedControlDispatcher() {
        RecordingSchedule schedules = new RecordingSchedule();
        RecordingSupervision supervision = new RecordingSupervision();
        RecordingResults results = new RecordingResults();
        HomecomingControlDispatcher dispatcher = new HomecomingControlDispatcher(
                schedules, supervision, results,
                (timeline, owner) -> "配置伴侣名");
        List<HomecomingControlParser.ControlEvent> controls =
                HomecomingControlParser.parse(
                        "[REMINDER:2030-01-02T09:00|喝水]"
                                + "[APP_TEMP_UNLOCK:group-video|10|处理事情]").events;

        dispatcher.apply(
                "schedule:execution-one",
                "main_private",
                "main",
                controls,
                2_000L);

        assertEquals(1, schedules.types.size());
        assertEquals("reminder", schedules.types.get(0));
        assertEquals(1, supervision.types.size());
        assertEquals(1, results.systemTexts.size());
        assertTrue(results.systemTexts.get(0).contains("配置伴侣名"));
    }

    private static HomecomingChatEngine.ChatCommand command() {
        return new HomecomingChatEngine.ChatCommand(
                "request-one", "main_private", "main", "user",
                "hello", "route", "model", "", "");
    }

    private static final class RecordingSchedule
            implements HomecomingControlDispatcher.SchedulePort {
        final List<String> types = new ArrayList<>();

        @Override
        public HomecomingScheduleCommandHandler.ApplyResult apply(
                String requestId,
                int index,
                HomecomingControlParser.ControlEvent event,
                String ownerId,
                String timelineId,
                long now) {
            types.add(event.type);
            if ("alarm".equals(event.type)) {
                return new HomecomingScheduleCommandHandler.ApplyResult(
                        "created", "schedule-one", true);
            }
            return new HomecomingScheduleCommandHandler.ApplyResult(
                    "deferred", "", false);
        }
    }

    private static final class RecordingSupervision
            implements HomecomingControlDispatcher.SupervisionPort {
        final List<String> types = new ArrayList<>();

        @Override
        public HomecomingSupervisionCommandHandler.ApplyResult apply(
                String requestId,
                int index,
                HomecomingControlParser.ControlEvent event,
                String ownerId,
                long now) {
            types.add(event.type);
            if ("app_supervision".equals(event.type)) {
                return new HomecomingSupervisionCommandHandler.ApplyResult(
                        "applied", "command-one", "lock",
                        20, "短视频", true);
            }
            return new HomecomingSupervisionCommandHandler.ApplyResult(
                    "deferred", "", "", 0, "", false);
        }
    }

    private static final class RecordingResults
            implements HomecomingControlDispatcher.ResultPort {
        final List<HomecomingControlParser.ControlEvent> deferred =
                new ArrayList<>();
        final List<String> systemTexts = new ArrayList<>();

        @Override
        public void defer(
                String requestId,
                HomecomingControlParser.ControlEvent event,
                long now) {
            deferred.add(event);
        }

        @Override
        public void system(
                String resultRequestId,
                String timelineId,
                String text,
                long now) {
            systemTexts.add(text);
        }
    }
}
