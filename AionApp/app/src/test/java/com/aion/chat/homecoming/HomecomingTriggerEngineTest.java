package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

public class HomecomingTriggerEngineTest {
    private static final long NOW = 1_000_000L;
    private static final long TRIGGER = 900_000L;

    @Test
    public void claimsBuildsCallsModelCommitsTextThenCompletesExecution() {
        Harness harness = new Harness(schedule("alarm"));
        harness.gateway.reply = "该起床了";

        harness.engine().execute(
                "schedule-one", TRIGGER, "route", "model", harness.result);

        assertEquals(Arrays.asList(
                "claim", "context", "gateway", "message", "complete"),
                harness.events);
        assertEquals("该起床了", harness.messageText);
        assertEquals("message-one", harness.result.messageId);
        assertEquals("该起床了", harness.result.text);
        assertNull(harness.result.failure);
    }

    @Test
    public void duplicateCompleteExecutionNeverCallsModelAgain() {
        Harness harness = new Harness(schedule("alarm"));
        harness.claimed = false;
        harness.claimState = "complete";

        harness.engine().execute(
                "schedule-one", TRIGGER, "route", "model", harness.result);

        assertEquals(Arrays.asList("claim"), harness.events);
        assertTrue(harness.result.duplicate);
        assertEquals(0, harness.gateway.requests);
    }

    @Test
    public void modelFailureAndEmptyFinalNeverCreateMessageOrComplete() {
        Harness failure = new Harness(schedule("reminder"));
        failure.gateway.failure = "MODEL_TIMEOUT";
        failure.engine().execute(
                "schedule-one", TRIGGER, "route", "model", failure.result);
        assertEquals("MODEL_TIMEOUT", failure.result.failure);
        assertNull(failure.messageText);
        assertEquals("MODEL_TIMEOUT", failure.failedDiagnostic);

        Harness empty = new Harness(schedule("alarm"));
        empty.gateway.reply = "[SCHEDULE_LIST]";
        empty.engine().execute(
                "schedule-one", TRIGGER, "route", "model", empty.result);
        assertEquals("empty_model_reply", empty.result.failure);
        assertNull(empty.messageText);
    }

    @Test
    public void timedMonitorTruthfullyStatesWhenNoImageExists() {
        Harness harness = new Harness(schedule("monitor"));
        harness.gateway.reply = "目前看不到画面，我先按现有状态提醒你。";

        harness.engine().execute(
                "schedule-one", TRIGGER, "route", "model", harness.result);

        assertTrue(harness.triggerText.contains("[定时监控触发]"));
        assertTrue(harness.triggerText.contains("本次没有可用的实时摄像头画面"));
        assertTrue(!harness.triggerText.contains("已经看到"));
    }

    @Test
    public void inactiveUnknownOrMismatchedScheduleIsRejectedBeforeClaim() {
        Harness missing = new Harness(null);
        missing.engine().execute(
                "schedule-one", TRIGGER, "route", "model", missing.result);
        assertEquals("unknown_schedule", missing.result.failure);
        assertTrue(missing.events.isEmpty());

        Harness mismatch = new Harness(schedule("alarm"));
        mismatch.engine().execute(
                "schedule-one", TRIGGER + 1L, "route", "model", mismatch.result);
        assertEquals("trigger_mismatch", mismatch.result.failure);
        assertTrue(mismatch.events.isEmpty());
    }

    @Test
    public void hiddenScheduleControlAppliesOnlyAfterMessageCommit() {
        Harness harness = new Harness(schedule("alarm"));
        harness.gateway.reply =
                "提醒完成[ALARM:2030-01-02T08:00|再次起床]";

        harness.engine().execute(
                "schedule-one", TRIGGER, "route", "model", harness.result);

        assertEquals("提醒完成", harness.messageText);
        assertEquals(Arrays.asList(
                "claim", "context", "gateway", "message", "controls", "complete"),
                harness.events);
        assertEquals("alarm", harness.controls.get(0).type);
    }

    @Test
    public void monitorSupervisionControlAppliesOnlyAfterVisibleMessageCommit() {
        Harness harness = new Harness(schedule("monitor"));
        harness.gateway.reply =
                "先休息一下[APP_LOCK:group-video|20|暂时休息]";

        harness.engine().execute(
                "schedule-one", TRIGGER, "route", "model", harness.result);

        assertEquals("先休息一下", harness.messageText);
        assertEquals(Arrays.asList(
                "claim", "context", "gateway", "message", "controls", "complete"),
                harness.events);
        assertEquals("app_supervision", harness.controls.get(0).type);
    }

    private static HomecomingScheduleRepository.Schedule schedule(String type) {
        return new HomecomingScheduleRepository.Schedule(
                "schedule-one", type, TRIGGER, "测试内容",
                "main", "main_private", "", "active", 1L, 1L, null);
    }

    private static final class Harness implements
            HomecomingTriggerEngine.SchedulePort,
            HomecomingTriggerEngine.ContextPort,
            HomecomingTriggerEngine.MessagePort,
            HomecomingTriggerEngine.ControlPort {
        final HomecomingScheduleRepository.Schedule schedule;
        final List<String> events = new ArrayList<>();
        final List<HomecomingControlParser.ControlEvent> controls = new ArrayList<>();
        final FakeGateway gateway = new FakeGateway(events);
        final RecordingResult result = new RecordingResult();
        boolean claimed = true;
        String claimState = "claimed";
        String messageText;
        String triggerText;
        String failedDiagnostic;

        Harness(HomecomingScheduleRepository.Schedule schedule) {
            this.schedule = schedule;
        }

        HomecomingTriggerEngine engine() {
            return new HomecomingTriggerEngine(
                    this, this, gateway, this, this, () -> NOW);
        }

        @Override public HomecomingScheduleRepository.Schedule find(String id) {
            return schedule;
        }

        @Override
        public HomecomingScheduleRepository.ExecutionClaim claim(
                String scheduleId, long triggerAt, long now) {
            events.add("claim");
            return new HomecomingScheduleRepository.ExecutionClaim(
                    scheduleId + ":" + triggerAt,
                    scheduleId,
                    triggerAt,
                    claimed,
                    claimState);
        }

        @Override
        public void complete(String executionId, String messageId, long now) {
            events.add("complete");
        }

        @Override
        public void fail(String executionId, String diagnostic, long now) {
            failedDiagnostic = diagnostic;
        }

        @Override
        public List<HomecomingModelGateway.ChatMessage> build(
                HomecomingScheduleRepository.Schedule schedule,
                String trigger,
                long now) {
            events.add("context");
            triggerText = trigger;
            return Arrays.asList(
                    new HomecomingModelGateway.ChatMessage("system", "configured"),
                    new HomecomingModelGateway.ChatMessage("user", trigger));
        }

        @Override
        public String commit(
                HomecomingScheduleRepository.Schedule schedule,
                String executionId,
                String completeText,
                long now) {
            events.add("message");
            messageText = completeText;
            return "message-one";
        }

        @Override
        public void apply(
                String executionId,
                HomecomingScheduleRepository.Schedule schedule,
                List<HomecomingControlParser.ControlEvent> values,
                long now) {
            if (!values.isEmpty()) {
                events.add("controls");
                controls.addAll(values);
            }
        }
    }

    private static final class FakeGateway
            implements HomecomingTriggerEngine.GatewayPort {
        final List<String> events;
        int requests;
        String reply = "完成";
        String failure;
        FakeGateway(List<String> events) {
            this.events = events;
        }
        @Override
        public void stream(
                String requestId,
                String routeId,
                String modelId,
                List<HomecomingModelGateway.ChatMessage> messages,
                HomecomingModelGateway.StreamObserver observer) {
            requests++;
            events.add("gateway");
            if (failure != null) observer.onFailure(failure);
            else observer.onComplete(reply);
        }
    }

    private static final class RecordingResult
            implements HomecomingTriggerEngine.Completion {
        String messageId;
        String text;
        String failure;
        boolean duplicate;
        @Override public void onComplete(String messageId, String text) {
            this.messageId = messageId;
            this.text = text;
        }
        @Override public void onFailure(String code) {
            failure = code;
        }
        @Override public void onDuplicate() {
            duplicate = true;
        }
    }
}
