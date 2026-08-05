package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

public class HomecomingChatEngineTest {
    @Test
    public void userCommitsBeforeModelAndAssistantOnlyAfterComplete() {
        EventRepository repository = new EventRepository();
        FakeGateway gateway = new FakeGateway(repository.events, "完整回答", null);
        HomecomingChatEngine engine = engine(repository, gateway);
        RecordingObserver observer = new RecordingObserver();

        engine.send(command("request-one", "main_private", "main"), observer);

        assertEquals(Arrays.asList(
                "context", "user", "gateway", "assistant", "controls"),
                repository.events);
        assertEquals("完整回答", repository.assistantText);
        assertEquals("完整回答", observer.complete);
        assertEquals("saved-assistant-id", observer.messageId);
    }

    @Test
    public void failureAndCancellationNeverCommitPartialAssistant() {
        EventRepository failedRepo = new EventRepository();
        FakeGateway failedGateway = new FakeGateway(
                failedRepo.events, null, "model_request_failed");
        RecordingObserver failedObserver = new RecordingObserver();
        engine(failedRepo, failedGateway)
                .send(command("request-fail", "main_private", "main"), failedObserver);

        assertNull(failedRepo.assistantText);
        assertEquals("model_request_failed", failedObserver.failure);
        assertEquals("request-fail:model_request_failed", failedRepo.failed);

        EventRepository stoppedRepo = new EventRepository();
        FakeGateway stoppedGateway = new FakeGateway(
                stoppedRepo.events, null, "cancelled");
        RecordingObserver stoppedObserver = new RecordingObserver();
        HomecomingChatEngine stoppedEngine = engine(stoppedRepo, stoppedGateway);
        stoppedEngine.send(
                command("request-stop", "main_private", "main"), stoppedObserver);
        assertNull(stoppedRepo.assistantText);
        assertEquals("cancelled", stoppedObserver.failure);
    }

    @Test
    public void allowlistedControlIsHiddenFromSavedAndVisibleReply() {
        EventRepository repository = new EventRepository();
        FakeGateway gateway = new FakeGateway(
                repository.events,
                "我记住了[ALARM:2030-01-02T08:00|起床]",
                null);
        RecordingObserver observer = new RecordingObserver();

        engine(repository, gateway)
                .send(command("request-control", "group", "second"), observer);

        assertEquals("我记住了", repository.assistantText);
        assertEquals("我记住了", observer.complete);
        assertEquals("alarm", repository.controls.get(0).type);
        assertEquals("second", repository.assistantSender);
    }

    @Test
    public void followupResponderBuildsFreshContextWithoutDuplicatingTheUser() {
        EventRepository repository = new EventRepository();
        FakeGateway gateway = new FakeGateway(
                repository.events, "second reply", null);
        HomecomingChatEngine engine = engine(repository, gateway);
        RecordingObserver observer = new RecordingObserver();

        engine.send(new HomecomingChatEngine.ChatCommand(
                "turn:second", "group", "second", "user", "hello",
                "route", "model", "", "", false), observer);

        assertEquals(Arrays.asList(
                "context", "prepare", "gateway", "assistant", "controls"),
                repository.events);
        assertEquals("second reply", observer.complete);
    }

    private static HomecomingChatEngine engine(
            EventRepository repository, FakeGateway gateway) {
        return new HomecomingChatEngine(
                repository,
                (timeline, owner, trigger, now, location) -> {
                    repository.events.add("context");
                    return Arrays.asList(
                            new HomecomingModelGateway.ChatMessage("system", "configured"),
                            new HomecomingModelGateway.ChatMessage("user", trigger));
                },
                gateway,
                () -> 100L);
    }

    private static HomecomingChatEngine.ChatCommand command(
            String requestId, String timeline, String owner) {
        return new HomecomingChatEngine.ChatCommand(
                requestId, timeline, owner, "user", "hello",
                "route", "model", "", "");
    }

    private static final class EventRepository implements HomecomingChatEngine.RepositoryPort {
        final List<String> events = new ArrayList<>();
        final List<HomecomingControlParser.ControlEvent> controls = new ArrayList<>();
        String assistantText;
        String assistantSender;
        String failed;

        @Override public void commitUser(HomecomingChatEngine.ChatCommand command, long now) {
            events.add("user");
        }
        @Override public void prepareAssistant(
                HomecomingChatEngine.ChatCommand command, long now) {
            events.add("prepare");
        }
        @Override public String commitAssistant(HomecomingChatEngine.ChatCommand command,
                String text, long now) {
            events.add("assistant");
            assistantText = text;
            assistantSender = command.responderOwner;
            return "saved-assistant-id";
        }
        @Override public void fail(String requestId, String code) {
            failed = requestId + ":" + code;
        }
        @Override public void recordControls(String requestId,
                List<HomecomingControlParser.ControlEvent> values) {
            events.add("controls");
            controls.addAll(values);
        }
    }

    private static final class FakeGateway implements HomecomingChatEngine.GatewayPort {
        final List<String> events;
        final String reply;
        final String failure;
        FakeGateway(List<String> events, String reply, String failure) {
            this.events = events;
            this.reply = reply;
            this.failure = failure;
        }
        @Override
        public void stream(HomecomingModelGateway.ChatRequest request,
                HomecomingModelGateway.StreamObserver observer) {
            events.add("gateway");
            if (failure != null) observer.onFailure(failure);
            else observer.onComplete(reply);
        }
        @Override public void cancel(String requestId) { }
    }

    private static final class RecordingObserver implements HomecomingChatEngine.Observer {
        String complete;
        String messageId;
        String failure;
        @Override public void onChunk(String text) { }
        @Override public void onComplete(String messageId, String text) {
            this.messageId = messageId;
            complete = text;
        }
        @Override public void onFailure(String code) { failure = code; }
    }
}
